from bot import check_intraday_signals
import datetime
import pytz

# Mock datetime to force the time to be 10:00 AM IST so it bypasses the time check
class MockDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return super().now(tz).replace(hour=10, minute=0)

import bot
bot.datetime.datetime = MockDatetime

print("Running check_intraday_signals()...")
bot.check_intraday_signals()
print("Done.")
