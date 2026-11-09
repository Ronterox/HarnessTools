"""A fake nvidia-smi for the labelling tests: `python fakesmi.py STATE --query-gpu=temperature.gpu,memory.free ...`
prints "TEMP, FREE" from the JSON file STATE ({"temp": °C, "free": MiB}) and counts its calls in STATE.calls."""
import json, sys

with open(sys.argv[1] + '.calls', 'a') as fh:
    fh.write('1\n')
with open(sys.argv[1]) as fh:
    s = json.load(fh)
print(f"{s['temp']}, {s['free']}")
