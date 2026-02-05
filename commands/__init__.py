from .voice import *
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# Import handle_commands directly from commands.py, not from the package.
from commands_main import handle_commands
