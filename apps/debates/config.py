# apps/debates/config.py
#
# Debates: opened and retired by proposal (a debates.<id> registry entry), argued in signed messages the
# Worker queues (DEBATES.md). No chain events of its own: the DAO module reports the registry changes.

APP_NAME = "debates"

BPS = 10000              # a share of voting power, in basis points
MAX_TEXT_BYTES = 4096    # an argument's text, UTF-8
MAX_OPEN = 20            # open debates per DAO
DAILY_QUOTA = 2000       # accepted messages per DAO per day (UTC)
POLL_SECONDS = 2         # between reads of the Worker queue
