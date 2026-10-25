"""Train and save the bundled Nasdaq-100 research models."""
import json
from server import forecast_nasdaq

if __name__ == "__main__":
    result = forecast_nasdaq({"instrument": "Nasdaq-100 (FRED/Nasdaq daily series)"})
    print(json.dumps({k:v for k,v in result.items() if k not in ("learning_trace","holdout_trace")}, indent=2))
