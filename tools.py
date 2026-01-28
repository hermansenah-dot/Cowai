from datetime import datetime
import pytz

def get_current_time(tz_name="Europe/Copenhagen"):
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    return now.strftime("%H:%M")
