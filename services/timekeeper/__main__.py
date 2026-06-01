import asyncio
import sys

from services.timekeeper.daemon import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
