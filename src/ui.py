#!/usr/bin/env python3
import os
import sys

launcher = os.path.expanduser("~/recogd-workspace/scripts/bazzite-launch.sh")
os.execv(launcher, [launcher] + sys.argv[1:])
