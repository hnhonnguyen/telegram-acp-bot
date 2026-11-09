"""Entry-point module, in case you use `python -m pp3`."""

import sys

from acp_chat_bot import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
