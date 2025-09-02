#!/bin/bash

# Install libraries required by Puppeteer/Chromium
apt-get update
apt-get install -y libgconf-2-4 libnss3 libxss1 libgdk-pixbuf2.0-0 libasound2 libatk-bridge2.0-0 libatk1.0-0 libgtk-3-0 libx11-xcb1 libdbus-1-3
