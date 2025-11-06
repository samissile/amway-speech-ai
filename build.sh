#!/usr/bin/env bash
set -e

# Install system dependencies
apt-get update
apt-get install -y ffmpeg libavcodec-extra

# Install Python dependencies
pip install -r requirements.txt