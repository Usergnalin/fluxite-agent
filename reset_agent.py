#!/usr/bin/env python3
import os
import shutil
import sys

def main():
    # Files and directories to remove
    to_remove = [
        "agent.key",
        "agent_id.txt",
        "servers.json",
        "servers",
        "tmp",
        "logs",
    ]

    for item in to_remove:
        if os.path.exists(item):
            try:
                if os.path.isdir(item):
                    shutil.rmtree(item)
                    print(f"Removed directory: {item}")
                else:
                    os.remove(item)
                    print(f"Removed file: {item}")
            except Exception as e:
                print(f"Error removing {item}: {e}")
        else:
            print(f"Not found, skipping: {item}")

    print("\nReset complete. The agent will start fresh on the next run.")

if __name__ == "__main__":
    main()
