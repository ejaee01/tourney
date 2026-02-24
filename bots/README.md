# Bots

Bot engines live in `bots/`. Each engine is a Python module (single file) or package (folder) that registers itself at import time via `bots.registry.register()`.

If a bot engine errors or returns an illegal move, the server falls back to the default engine (`random_capture`).

## Single-file bot engine

Create a file like `bots/my_engine.py`:

```py
import random
import chess

from .registry import BotEngine, register


def choose_move(board: chess.Board) -> chess.Move:
    return random.choice(list(board.legal_moves))


register(
    BotEngine(
        key="my_engine",
        name="My Engine",
        choose_move=choose_move,
    )
)
```

## Multi-file bot engine

Create a folder like `bots/my_engine/` with a `bots/my_engine/__init__.py` that imports your code and calls `register(...)`.

## Connect an engine to a bot account

- Admin → **Bots**: pick an engine while creating a new bot account
- Admin → **Players**: for BOT accounts, pick an engine and click **Set Engine**

If you add new engines, redeploy/restart the server so they get imported and show up in the engine list.

