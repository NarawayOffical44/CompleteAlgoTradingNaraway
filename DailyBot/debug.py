import sys
print("1 Python OK", flush=True)
import json, logging, os, time
print("2 stdlib OK", flush=True)
import requests
print("3 requests OK", flush=True)
from dotenv import load_dotenv; load_dotenv()
print("4 dotenv OK", flush=True)
import ccxt
print("5 ccxt OK", flush=True)
import pandas as pd
print("6 pandas OK", flush=True)
import numpy as np
print("7 numpy OK", flush=True)
print("ALL IMPORTS OK", flush=True)
