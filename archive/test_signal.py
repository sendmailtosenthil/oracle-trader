import os
from donchian import evaluate_donchian_intraday
import pprint

def test():
    print("Testing NIFTY vs GOLD donchian evaluation...")
    res = evaluate_donchian_intraday('NIFTYBEES.NS', 'GOLDBEES.NS', 25)
    if res:
        print("Live Ratio:", res['live_ratio'])
        print("Upper Band:", res['upper'])
        print("Lower Band:", res['lower'])
        print("Signal    :", res['signal'])
    else:
        print("Result is None")

if __name__ == "__main__":
    test()
