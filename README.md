# bedhelper
A containerized Discord-based assistance bot that helps remotely manage a certain Facebook embedder website. Requires `/var/run/docker.sock` mounted to the container, the target service compose configuration mounted to `/compose/compose.yaml`, `--pid=host` with these system capabilities: `SYS_ADMIN`, `SYS_CHROOT` and `SYS_PTRACE`.

## Environment variables
- `SERVICE_UNIT`: specify the name of the service unit name under `systemd`.
- `TARGET_COMPOSE_SERVICE`: optional Compose service name whose container uptime is shown in the bot presence. Defaults to the service matching `SERVICE_UNIT`; if the project has exactly one running container, that container is used.
- `DISCORD_TOKEN`: token of the Discord bot.
- `DISCORD_GUILD_ID`: ID of the server that the Discord bot should be in.
- `ALLOWED_USER_IDS`: IDs of the users that are allowed to use the bot, seperated by commas.
- `LOG_CHANNEL_ID`: optional ID of the channel where new service journal logs are sent in real time.
- `LOG_LEVEL`: logging levels, default to `INFO`, more info [here](https://dev.to/mcheremnov/python-logging-levels-50g).

## Commands
- `/status`, `/enable`, `/disable`, `/stop`, `/start`, `/restart`, `/update` and `/prune`.

When `LOG_CHANNEL_ID` is set, the bot sends new `SERVICE_UNIT` journal entries to that channel as they arrive.

The bot presence stays set to DND and shows host CPU/RAM usage plus the selected running Compose container's uptime.