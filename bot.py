from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import discord
from discord import app_commands

discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

LOGGER = logging.getLogger("bedhelper")

SERVICE_UNIT = os.environ.get("SERVICE_UNIT", "facebed")
TARGET_COMPOSE_SERVICE = os.environ.get("TARGET_COMPOSE_SERVICE", "").strip()
COMPOSE_FILE = "/compose/compose.yaml"
COMPOSE_DIRECTORY = "/compose"
DOCKER_SOCKET = "unix:///var/run/docker.sock"
NSENTER = "/usr/bin/nsenter"

SYSTEMCTL_TIMEOUT_SECONDS = 120
DOCKER_COMMAND_TIMEOUT_SECONDS = 300
PROGRESS_EDIT_INTERVAL_SECONDS = 2.0
PROGRESS_LINE_COUNT = 20
LOG_BATCH_LINE_COUNT = 20
LOG_FLUSH_INTERVAL_SECONDS = 2.0
LOG_RETRY_INTERVAL_SECONDS = 5.0
PRESENCE_REFRESH_DELAY_SECONDS = 5.0
PRESENCE_UPDATE_INTERVAL_SECONDS = 60.0
CPU_SAMPLE_INTERVAL_SECONDS = 1.0
DOCKER_PRESENCE_TIMEOUT_SECONDS = 15.0
DISCORD_MESSAGE_LIMIT = 1900

NO_MENTIONS = discord.AllowedMentions.none()


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    guild_id: int
    allowed_user_ids: frozenset[int]
    log_channel_id: int | None

    @classmethod
    def from_environment(cls) -> "Settings":
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN must be set")

        guild_id = _read_snowflake("DISCORD_GUILD_ID")
        raw_user_ids = os.environ.get("ALLOWED_USER_IDS", "")
        user_id_parts = [part.strip() for part in raw_user_ids.split(",") if part.strip()]
        if not user_id_parts:
            raise RuntimeError("ALLOWED_USER_IDS must contain at least one Discord user ID")
        if any(not part.isascii() or not part.isdecimal() for part in user_id_parts):
            raise RuntimeError("ALLOWED_USER_IDS must be a comma-separated list of integer IDs")

        try:
            user_ids = frozenset(int(part, 10) for part in user_id_parts)
        except ValueError as exc:
            raise RuntimeError("ALLOWED_USER_IDS must be a comma-separated list of integer IDs") from exc
        if any(user_id <= 0 for user_id in user_ids):
            raise RuntimeError("ALLOWED_USER_IDS must contain positive integer IDs")

        return cls(
            discord_token=token,
            guild_id=guild_id,
            allowed_user_ids=user_ids,
            log_channel_id=_read_optional_snowflake("LOG_CHANNEL_ID"),
        )


def _read_snowflake(name: str) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise RuntimeError(f"{name} must be a positive integer Discord ID")
    try:
        value = int(raw_value, 10)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer Discord ID") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer Discord ID")
    return value


def _read_optional_snowflake(name: str) -> int | None:
    if not os.environ.get(name, "").strip():
        return None
    return _read_snowflake(name)


class HostCommandError(RuntimeError):

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output.strip()


def _format_code_block(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    safe_text = text.replace("```", "'''").strip() or "Command completed with no output."
    opening = "```text\n"
    closing = "\n```"
    available = limit - len(opening) - len(closing)
    if len(safe_text) > available:
        marker = "[earlier output omitted]\n"
        safe_text = marker + safe_text[-(available - len(marker)) :]
    return f"{opening}{safe_text}{closing}"


def _systemctl_command(action: str) -> list[str]:
    prefix = [
        NSENTER,
        "-t",
        "1",
        "-m",
        "-r",
        "--",
        "systemctl",
        "--no-pager",
        "--no-ask-password",
    ]
    if action == "show":
        return prefix + [
            "show",
            "--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,ActiveEnterTimestamp",
            SERVICE_UNIT,
        ]
    if action in {"start", "stop", "restart", "enable", "disable"}:
        return prefix + [action, SERVICE_UNIT]
    raise ValueError(f"Unsupported fixed systemctl action: {action}")


def _journal_command() -> list[str]:
    return [
        NSENTER,
        "-t",
        "1",
        "-m",
        "-r",
        "--",
        "journalctl",
        "--follow",
        "--lines=0",
        "--no-pager",
        "--output=cat",
        "--unit",
        SERVICE_UNIT,
    ]


def _docker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["DOCKER_HOST"] = DOCKER_SOCKET
    environment.pop("DOCKER_CONTEXT", None)
    environment.pop("DOCKER_TLS_VERIFY", None)
    environment.pop("DOCKER_CERT_PATH", None)
    return environment


def _compose_config_command() -> list[str]:
    return [
        "docker",
        "--host",
        DOCKER_SOCKET,
        "compose",
        "--file",
        COMPOSE_FILE,
        "--project-directory",
        COMPOSE_DIRECTORY,
        "config",
        "--format",
        "json",
    ]


def _compose_ps_command() -> list[str]:
    return [
        "docker",
        "--host",
        DOCKER_SOCKET,
        "compose",
        "--file",
        COMPOSE_FILE,
        "--project-directory",
        COMPOSE_DIRECTORY,
        "ps",
        "--format",
        "json",
    ]


async def _run_docker_command(
    command: list[str], timeout: float = DOCKER_COMMAND_TIMEOUT_SECONDS
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=COMPOSE_DIRECTORY,
            env=_docker_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise HostCommandError(f"Could not start Docker: {exc}") from exc

    try:
        output_bytes, error_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        output_bytes, error_bytes = await process.communicate()
        output = "\n".join(
            part
            for part in (
                output_bytes.decode("utf-8", errors="replace").strip(),
                error_bytes.decode("utf-8", errors="replace").strip(),
            )
            if part
        )
        raise HostCommandError(
            f"Docker command timed out after {timeout:g} seconds",
            output,
        ) from exc
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise

    output = output_bytes.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        error_output = error_bytes.decode("utf-8", errors="replace").strip()
        raise HostCommandError(
            f"Docker command exited with code {process.returncode}",
            "\n".join(part for part in (output, error_output) if part),
        )
    return output


async def _prune_compose_images() -> tuple[str, str]:
    config_output = await _run_docker_command(_compose_config_command())
    try:
        config = json.loads(config_output)
    except json.JSONDecodeError as exc:
        raise HostCommandError("Docker Compose returned invalid JSON configuration.") from exc

    project_name = config.get("name") if isinstance(config, dict) else None
    if not isinstance(project_name, str) or not project_name.strip():
        raise HostCommandError("Docker Compose configuration did not include a project name.")

    image_repositories: set[str] = set()
    services = config.get("services", {}) if isinstance(config, dict) else {}
    if isinstance(services, dict):
        for service in services.values():
            if isinstance(service, dict) and isinstance(service.get("image"), str):
                image_repositories.add(
                    _normalize_image_repository(service["image"])
                )

    matched_ids = await _dangling_image_ids_for_compose(
        project_name, image_repositories
    )
    outputs: list[str] = []
    for offset in range(0, len(matched_ids), 50):
        image_ids = matched_ids[offset : offset + 50]
        remove_command = [
            "docker",
            "--host",
            DOCKER_SOCKET,
            "image",
            "rm",
            "--no-prune",
            *image_ids,
        ]
        try:
            removal_output = await _run_docker_command(remove_command)
            outputs.append(
                removal_output
                or f"Removed {len(image_ids)} matching dangling image(s)."
            )
        except HostCommandError as exc:
            outputs.append(
                f"Some matching images could not be removed:\n{exc.output or exc}"
            )

    if not outputs:
        outputs.append(
            "No dangling images matched this Compose project or its configured image repositories."
        )

    return project_name, "\n".join(output for output in outputs if output)


def _normalize_image_repository(reference: str) -> str:
    repository = reference.strip().lower().split("@", 1)[0]
    slash = repository.rfind("/")
    colon = repository.rfind(":")
    if colon > slash:
        repository = repository[:colon]

    parts = repository.split("/")
    first = parts[0]
    if first == "index.docker.io":
        parts[0] = "docker.io"
        first = "docker.io"
    elif "." not in first and ":" not in first and first != "localhost":
        parts.insert(0, "docker.io")
        first = "docker.io"

    if first == "docker.io" and len(parts) == 2:
        parts.insert(1, "library")
    return "/".join(parts)


async def _dangling_image_ids_for_compose(
    project_name: str,
    image_repositories: set[str],
) -> list[str]:
    list_output = await _run_docker_command(
        [
            "docker",
            "--host",
            DOCKER_SOCKET,
            "image",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            "dangling=true",
        ],
        timeout=DOCKER_PRESENCE_TIMEOUT_SECONDS,
    )
    dangling_ids = [line.strip() for line in list_output.splitlines() if line.strip()]
    matching_ids: list[str] = []
    for offset in range(0, len(dangling_ids), 50):
        image_ids = dangling_ids[offset : offset + 50]
        inspect_output = await _run_docker_command(
            [
                "docker",
                "--host",
                DOCKER_SOCKET,
                "image",
                "inspect",
                "--format",
                "{{.Id}}\t{{json .RepoDigests}}\t{{json .Config.Labels}}",
                *image_ids,
            ],
            timeout=DOCKER_PRESENCE_TIMEOUT_SECONDS,
        )
        for line in inspect_output.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3:
                continue
            image_id, raw_digests, raw_labels = fields
            try:
                digests = json.loads(raw_digests)
                labels = json.loads(raw_labels)
            except json.JSONDecodeError:
                continue
            is_project_image = (
                isinstance(labels, dict)
                and labels.get("com.docker.compose.project") == project_name
            )
            is_configured_image = isinstance(digests, list) and any(
                isinstance(digest, str)
                and _normalize_image_repository(digest) in image_repositories
                for digest in digests
            )
            if is_project_image or is_configured_image:
                matching_ids.append(image_id)
    return matching_ids


async def _target_container_uptime_seconds() -> float | None:
    output = await _run_docker_command(
        _compose_ps_command(), timeout=DOCKER_PRESENCE_TIMEOUT_SECONDS
    )
    containers: list[dict[str, object]] = []
    try:
        for line in output.splitlines():
            if line.strip():
                record = json.loads(line)
                if isinstance(record, dict):
                    containers.append(record)
    except json.JSONDecodeError as exc:
        raise HostCommandError("Docker Compose returned invalid container data.") from exc

    running = [
        container
        for container in containers
        if str(container.get("State", "")).lower() == "running"
        and isinstance(container.get("ID"), str)
    ]
    if TARGET_COMPOSE_SERVICE:
        target_service = TARGET_COMPOSE_SERVICE
    else:
        target_service = SERVICE_UNIT.removesuffix(".service")

    matching = [
        container
        for container in running
        if container.get("Service") == target_service
    ]
    if matching:
        target = matching[0]
    elif TARGET_COMPOSE_SERVICE:
        return None
    elif len(running) == 1:
        target = running[0]
    else:
        return None

    container_id = target["ID"]
    started_at_output = await _run_docker_command(
        [
            "docker",
            "--host",
            DOCKER_SOCKET,
            "inspect",
            "--type",
            "container",
            "--format",
            "{{.State.StartedAt}}",
            str(container_id),
        ],
        timeout=DOCKER_PRESENCE_TIMEOUT_SECONDS,
    )
    try:
        started_at = datetime.fromisoformat(started_at_output.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostCommandError("Docker returned an invalid container start time.") from exc
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())


async def _run_systemctl(action: str) -> str:
    command = _systemctl_command(action)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise HostCommandError(f"Could not start nsenter/systemctl: {exc}") from exc

    try:
        output_bytes, _ = await asyncio.wait_for(
            process.communicate(), timeout=SYSTEMCTL_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise HostCommandError(
            f"systemctl {action} timed out after {SYSTEMCTL_TIMEOUT_SECONDS} seconds"
        ) from exc

    output = output_bytes.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise HostCommandError(
            f"systemctl {action} exited with code {process.returncode}", output
        )
    return output


def _parse_properties(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def _format_status(output: str) -> str:
    properties = _parse_properties(output)
    if not properties:
        return output or "systemctl returned no unit properties."

    names = (
        ("Id", "Unit"),
        ("LoadState", "Loaded"),
        ("ActiveState", "Active"),
        ("SubState", "Substate"),
        ("Result", "Result"),
        ("ExecMainStatus", "Main process exit code"),
        ("ActiveEnterTimestamp", "Active since"),
    )
    return "\n".join(
        f"{label}: {properties[key]}" for key, label in names if properties.get(key)
    )


def _read_cpu_counters() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="ascii") as cpu_file:
            fields = cpu_file.readline().split()
        if not fields or fields[0] != "cpu":
            return None
        values = [int(value) for value in fields[1:9]]
    except (OSError, ValueError):
        return None

    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _cpu_usage_percent(
    previous: tuple[int, int] | None,
    current: tuple[int, int] | None,
) -> int | None:
    if previous is None or current is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)))


def _read_memory_usage_percent() -> int | None:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as memory_file:
            for line in memory_file:
                key, separator, raw_value = line.partition(":")
                if separator and key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(raw_value.split()[0])
    except (OSError, ValueError, IndexError):
        return None

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable")
    if total <= 0 or available is None:
        return None
    return round(max(0.0, min(100.0, (total - available) * 100 / total)))


def _format_uptime(seconds: float) -> str:
    elapsed = max(0, int(seconds))
    units = (
        (365 * 24 * 60 * 60, "year", "years"),
        (30 * 24 * 60 * 60, "month", "months"),
        (24 * 60 * 60, "day", "days"),
        (60 * 60, "hour", "hours"),
        (60, "min", "mins"),
        (1, "sec", "secs"),
    )
    for unit_seconds, singular, plural in units:
        if elapsed >= unit_seconds:
            value = elapsed // unit_seconds
            return f"{value} {singular if value == 1 else plural}"
    return "0 secs"


def _render_presence(
    cpu_percent: int | None,
    memory_percent: int | None,
    uptime_seconds: float | None,
) -> str:
    cpu = f"{cpu_percent}%" if cpu_percent is not None else "n/a"
    memory = f"{memory_percent}%" if memory_percent is not None else "n/a"
    uptime = _format_uptime(uptime_seconds) if uptime_seconds is not None else "n/a"
    return f"CPU: {cpu} | RAM: {memory} | Up: {uptime}"


def _clean_log_line(raw_line: bytes) -> str:
    decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
    return "".join(
        character for character in decoded if character.isprintable() or character == "\t"
    )


def _render_pull_progress(state: str, lines: deque[str]) -> str:
    body = "\n".join(lines) or "Waiting for Docker Compose output..."
    body = body.replace("```", "'''")
    opening = f"**facebed image update - {state}**\n```text\n"
    closing = "\n```"
    available = DISCORD_MESSAGE_LIMIT - len(opening) - len(closing)
    if len(body) > available:
        marker = "[earlier output omitted]\n"
        body = marker + body[-(available - len(marker)) :]
    return f"{opening}{body}{closing}"


async def _edit_progress_message(message: discord.Message, content: str) -> None:
    try:
        await message.edit(content=content, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException:
        LOGGER.exception("Could not edit the Discord pull progress message")


def _render_service_log_messages(lines: list[str]) -> list[str]:
    opening = f"```text\n"
    closing = "\n```"
    available = DISCORD_MESSAGE_LIMIT - len(opening) - len(closing)
    messages: list[str] = []
    body = ""

    for original_line in lines:
        line = original_line.replace("```", "'''")
        while line:
            if body:
                if len(body) + 1 == available:
                    messages.append(f"{opening}{body}{closing}")
                    body = ""
                else:
                    body += "\n"

            take = min(len(line), available - len(body))
            body += line[:take]
            line = line[take:]
            if len(body) == available:
                messages.append(f"{opening}{body}{closing}")
                body = ""

    if body:
        messages.append(f"{opening}{body}{closing}")
    return messages


async def _send_service_log_lines(
    channel: discord.abc.Messageable,
    lines: list[str],
) -> None:
    for content in _render_service_log_messages(lines):
        await channel.send(content, allowed_mentions=NO_MENTIONS)


async def _pull_compose_images(
    message: discord.Message,
) -> tuple[int, deque[str]]:
    command = [
        "docker",
        "--host",
        DOCKER_SOCKET,
        "compose",
        "--file",
        COMPOSE_FILE,
        "--project-directory",
        COMPOSE_DIRECTORY,
        "--ansi",
        "never",
        "--progress",
        "plain",
        "pull",
        "--ignore-buildable",
    ]
    environment = os.environ.copy()
    environment["DOCKER_HOST"] = DOCKER_SOCKET
    environment.pop("DOCKER_CONTEXT", None)
    environment.pop("DOCKER_TLS_VERIFY", None)
    environment.pop("DOCKER_CERT_PATH", None)

    lines: deque[str] = deque(maxlen=PROGRESS_LINE_COUNT)
    state = "pulling images"
    await _edit_progress_message(message, _render_pull_progress(state, lines))

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=COMPOSE_DIRECTORY,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        lines.append(f"Could not start Docker Compose: {exc}")
        await _edit_progress_message(
            message, _render_pull_progress("pull failed", lines)
        )
        return 127, lines

    last_edit = 0.0
    try:
        assert process.stdout is not None
        while raw_line := await process.stdout.readline():
            line = _clean_log_line(raw_line)
            if line:
                lines.append(line)
            now = time.monotonic()
            if now - last_edit >= PROGRESS_EDIT_INTERVAL_SECONDS:
                await _edit_progress_message(
                    message, _render_pull_progress(state, lines)
                )
                last_edit = now
        return_code = await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise

    state = "images pulled" if return_code == 0 else f"pull failed (exit {return_code})"
    await _edit_progress_message(message, _render_pull_progress(state, lines))
    return return_code, lines


class ControlTree(app_commands.CommandTree):
    def __init__(self, client: discord.Client, settings: Settings) -> None:
        super().__init__(client)
        self.settings = settings

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id != self.settings.guild_id
            or interaction.user.id not in self.settings.allowed_user_ids
        ):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "You are not authorized to use this bot.",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        LOGGER.error(
            "Application command failed",
            exc_info=(type(original), original, original.__traceback__),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "The command failed. Check the bot container logs for details.",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
            else:
                await interaction.response.send_message(
                    "The command failed. Check the bot container logs for details.",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
        except discord.HTTPException:
            LOGGER.exception("Could not report an application-command error")


class ControlClient(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, status=discord.Status.dnd)
        self.settings = settings
        self.tree = ControlTree(self, settings)
        self._presence_task: asyncio.Task[None] | None = None
        self._pending_presence_refresh: asyncio.Task[None] | None = None
        self._log_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)
        commands = await self.tree.sync(guild=guild)
        LOGGER.info(
            "Synchronized %d slash commands to guild %s",
            len(commands),
            self.settings.guild_id,
        )
        self._presence_task = asyncio.create_task(
            self._update_presence(), name="presence-updater"
        )
        if self.settings.log_channel_id is not None:
            self._log_task = asyncio.create_task(
                self._forward_service_logs(), name="service-log-forwarder"
            )

    async def close(self) -> None:
        for task in (
            self._presence_task,
            self._pending_presence_refresh,
            self._log_task,
        ):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await super().close()

    async def _update_presence(self) -> None:
        await self.wait_until_ready()
        previous_cpu = _read_cpu_counters()
        while not self.is_closed():
            await asyncio.sleep(CPU_SAMPLE_INTERVAL_SECONDS)
            current_cpu = _read_cpu_counters()
            cpu_percent = _cpu_usage_percent(previous_cpu, current_cpu)
            if current_cpu is not None:
                previous_cpu = current_cpu

            await self._apply_presence(cpu_percent)
            await asyncio.sleep(PRESENCE_UPDATE_INTERVAL_SECONDS - CPU_SAMPLE_INTERVAL_SECONDS)

    def schedule_presence_refresh(self) -> None:
        if self.is_closed():
            return
        self.cancel_presence_refresh()
        self._pending_presence_refresh = asyncio.create_task(
            self._refresh_presence_after_delay(), name="presence-action-refresh"
        )

    def cancel_presence_refresh(self) -> None:
        if (
            self._pending_presence_refresh is not None
            and not self._pending_presence_refresh.done()
        ):
            self._pending_presence_refresh.cancel()
        self._pending_presence_refresh = None

    async def _refresh_presence_after_delay(self) -> None:
        try:
            await asyncio.sleep(PRESENCE_REFRESH_DELAY_SECONDS)
            if self.is_closed():
                return

            previous_cpu = _read_cpu_counters()
            await asyncio.sleep(CPU_SAMPLE_INTERVAL_SECONDS)
            current_cpu = _read_cpu_counters()
            while OPERATION_LOCK.locked() and not self.is_closed():
                await asyncio.sleep(0.1)
            if self.is_closed():
                return
            await self._apply_presence(_cpu_usage_percent(previous_cpu, current_cpu))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Could not refresh the bot's presence after a service action")

    async def _apply_presence(self, cpu_percent: int | None) -> None:
        memory_percent = _read_memory_usage_percent()
        try:
            uptime_seconds = await _target_container_uptime_seconds()
        except HostCommandError as exc:
            LOGGER.warning("Could not read target container uptime: %s", exc)
            uptime_seconds = None
        activity = discord.Game(
            name=_render_presence(
                cpu_percent,
                memory_percent,
                uptime_seconds,
            )
        )
        try:
            await self.change_presence(
                status=discord.Status.dnd,
                activity=activity,
            )
        except discord.DiscordException:
            LOGGER.exception("Could not update the bot's Discord presence")

    async def _forward_service_logs(self) -> None:
        channel_id = self.settings.log_channel_id
        if channel_id is None:
            return

        while not self.is_closed():
            process: asyncio.subprocess.Process | None = None
            read_task: asyncio.Task[bytes] | None = None
            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)
                if not hasattr(channel, "send"):
                    raise RuntimeError(
                        f"Discord channel {channel_id} cannot receive messages"
                    )

                process = await asyncio.create_subprocess_exec(
                    *_journal_command(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                if process.stdout is None:
                    raise RuntimeError("journalctl did not provide an output stream")

                LOGGER.info(
                    "Forwarding %s journal logs to Discord channel %s",
                    SERVICE_UNIT,
                    channel_id,
                )
                pending_lines: list[str] = []
                last_flush = time.monotonic()

                while not self.is_closed():
                    if read_task is None:
                        read_task = asyncio.create_task(process.stdout.readline())
                    completed, _ = await asyncio.wait(
                        (read_task,), timeout=LOG_FLUSH_INTERVAL_SECONDS
                    )
                    raw_line = None
                    if completed:
                        raw_line = read_task.result()
                        read_task = None

                    if raw_line == b"":
                        if pending_lines:
                            await _send_service_log_lines(channel, pending_lines)
                        return_code = await process.wait()
                        LOGGER.warning(
                            "journalctl for %s exited with code %d",
                            SERVICE_UNIT,
                            return_code,
                        )
                        break

                    if raw_line:
                        line = _clean_log_line(raw_line)
                        if line:
                            pending_lines.append(line)

                    now = time.monotonic()
                    if pending_lines and (
                        len(pending_lines) >= LOG_BATCH_LINE_COUNT
                        or now - last_flush >= LOG_FLUSH_INTERVAL_SECONDS
                    ):
                        await _send_service_log_lines(channel, pending_lines)
                        pending_lines.clear()
                        last_flush = now
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Could not forward host systemd service logs")
            finally:
                if read_task is not None and not read_task.done():
                    read_task.cancel()
                    try:
                        await read_task
                    except asyncio.CancelledError:
                        pass
                if process is not None and process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()

            if not self.is_closed():
                await asyncio.sleep(LOG_RETRY_INTERVAL_SECONDS)


OPERATION_LOCK = asyncio.Lock()


async def _acquire_operation(interaction: discord.Interaction) -> bool:
    if OPERATION_LOCK.locked():
        await interaction.response.send_message(
            "Another host operation is already running. Try again when it finishes.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return False
    await OPERATION_LOCK.acquire()
    return True


async def _run_unit_action(
    interaction: discord.Interaction,
    action: str,
    client: ControlClient,
) -> None:
    if not await _acquire_operation(interaction):
        return

    if action in {"start", "stop", "restart"}:
        client.cancel_presence_refresh()

    try:
        await interaction.response.defer(thinking=True, ephemeral=False)
        try:
            output = await _run_systemctl(action)
        except HostCommandError as exc:
            details = _format_code_block(exc.output or str(exc))
            await interaction.edit_original_response(
                content=f"FAILED: `{SERVICE_UNIT}` {action}.\n{details}",
                allowed_mentions=NO_MENTIONS,
            )
            return

        if action in {"start", "stop", "restart"}:
            client.schedule_presence_refresh()

        result = output or "systemctl completed successfully."
        await interaction.edit_original_response(
            content=f"SUCCESS: `{SERVICE_UNIT}` {action}.\n{_format_code_block(result)}",
            allowed_mentions=NO_MENTIONS,
        )
    finally:
        OPERATION_LOCK.release()


def _add_commands(client: ControlClient) -> None:
    guild = discord.Object(id=client.settings.guild_id)

    @client.tree.command(
        name="update",
        description="Pull facebed's Compose images and restart its systemd service.",
        guild=guild,
    )
    async def update(interaction: discord.Interaction) -> None:
        if not await _acquire_operation(interaction):
            return

        client.cancel_presence_refresh()

        try:
            channel = interaction.channel
            if channel is None or not hasattr(channel, "send"):
                await interaction.response.send_message(
                    "Use this command in a server text channel.",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            await interaction.response.defer(thinking=True, ephemeral=False)
            progress_message = await channel.send(
                _render_pull_progress("starting", deque()),
                allowed_mentions=NO_MENTIONS,
            )
            await interaction.edit_original_response(
                content=f"Image update started. Live output: {progress_message.jump_url}",
                allowed_mentions=NO_MENTIONS,
            )

            return_code, lines = await _pull_compose_images(progress_message)
            if return_code != 0:
                return

            lines.append("Images pulled successfully.")
            lines.append("Restarting the systemd service...")
            await _edit_progress_message(
                progress_message, _render_pull_progress("restarting service", lines)
            )
            try:
                await _run_systemctl("restart")
            except HostCommandError as exc:
                lines.append(f"Service restart failed: {exc}")
                if exc.output:
                    lines.append(exc.output)
                await _edit_progress_message(
                    progress_message,
                    _render_pull_progress("images pulled; restart failed", lines),
                )
                return

            lines.append(f"`{SERVICE_UNIT}` restarted successfully.")
            client.schedule_presence_refresh()
            await _edit_progress_message(
                progress_message, _render_pull_progress("complete", lines)
            )
        finally:
            OPERATION_LOCK.release()

    @client.tree.command(
        name="prune",
        description="Delete old dangling images from facebed's Compose project.",
        guild=guild,
    )
    async def prune(interaction: discord.Interaction) -> None:
        if not await _acquire_operation(interaction):
            return

        try:
            await interaction.response.defer(thinking=True, ephemeral=False)
            try:
                project_name, output = await _prune_compose_images()
            except HostCommandError as exc:
                details = _format_code_block(exc.output or str(exc))
                await interaction.edit_original_response(
                    content=f"Could not prune dangling images for the Compose project.\n{details}",
                    allowed_mentions=NO_MENTIONS,
                )
                return

            result = output or "No dangling images were removed."
            await interaction.edit_original_response(
                content=(
                    f"Dangling images for Compose project `{project_name}` pruned.\n"
                    f"{_format_code_block(result)}"
                ),
                allowed_mentions=NO_MENTIONS,
            )
        finally:
            OPERATION_LOCK.release()

    @client.tree.command(
        name="start",
        description="Start the facebed systemd service.",
        guild=guild,
    )
    async def start(interaction: discord.Interaction) -> None:
        await _run_unit_action(interaction, "start", client)

    @client.tree.command(
        name="stop",
        description="Stop the facebed systemd service.",
        guild=guild,
    )
    async def stop(interaction: discord.Interaction) -> None:
        await _run_unit_action(interaction, "stop", client)

    @client.tree.command(
        name="restart",
        description="Restart the facebed systemd service.",
        guild=guild,
    )
    async def restart(interaction: discord.Interaction) -> None:
        await _run_unit_action(interaction, "restart", client)

    @client.tree.command(
        name="enable",
        description="Start the facebed systemd service on server startup.",
        guild=guild,
    )
    async def enable(interaction: discord.Interaction) -> None:
        await _run_unit_action(interaction, "enable", client)

    @client.tree.command(
        name="disable",
        description="Do not start the facebed systemd service on server startup.",
        guild=guild,
    )
    async def disable(interaction: discord.Interaction) -> None:
        await _run_unit_action(interaction, "disable", client)

    @client.tree.command(
        name="status",
        description="Show the facebed systemd service status.",
        guild=guild,
    )
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=False)
        try:
            output = await _run_systemctl("show")
        except HostCommandError as exc:
            result = _format_code_block(exc.output or str(exc))
            await interaction.edit_original_response(
                content=f"Could not read `{SERVICE_UNIT}` status.\n{result}",
                allowed_mentions=NO_MENTIONS,
            )
            return

        await interaction.edit_original_response(
            content=f"**`{SERVICE_UNIT}` status**\n{_format_code_block(_format_status(output))}",
            allowed_mentions=NO_MENTIONS,
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_environment()
    except RuntimeError as exc:
        LOGGER.critical("Invalid configuration: %s", exc)
        raise SystemExit(2) from exc

    client = ControlClient(settings)
    _add_commands(client)
    client.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()