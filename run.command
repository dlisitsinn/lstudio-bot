#!/bin/bash
# Mac: дважды щёлкните этот файл. Настройки — файл .env в этой папке.
cd "$(dirname "$0")"
python3 updater.py
python3 main.py "$@"
