"""Put the project root on sys.path so eval tests can import agent/router/refusals/metrics."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
