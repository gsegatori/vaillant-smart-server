#!/bin/bash
# attiva il venv se esiste
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi
exec bash
