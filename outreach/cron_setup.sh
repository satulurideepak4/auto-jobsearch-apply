#!/bin/bash
# Adds cron job: runs pipeline every weekday at 9am IST (3:30 UTC)
# Usage: bash outreach/cron_setup.sh
PROJECT_DIR=$(pwd)
CRON_JOB="30 3 * * 1-5 cd $PROJECT_DIR && .venv/bin/python outreach/main.py >> outreach/pipeline.log 2>&1"
(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
echo "Cron job added successfully."
echo "Verify with: crontab -l"
echo "Pipeline will run every weekday at 9:00 AM IST"
