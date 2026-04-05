from flask import Flask, request, abort
import requests
import os
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import re
from datetime import datetime, time

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", os.getenv("account_sid", "")).strip().strip("'\"")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", os.getenv("auth_token", "")).strip().strip("'\"")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "").strip().strip("'\"")

if not WEATHER_API_KEY:
    print("[WARNING] WEATHER_API_KEY is not configured. Add it to .env as WEATHER_API_KEY=your_api_key")

if not TWILIO_AUTH_TOKEN or not TWILIO_ACCOUNT_SID:
    print("[WARNING] TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not configured. Incoming request validation will be skipped.")

# Initialize Twilio client for sending messages
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Store scheduled reminders (in production, use a database)
scheduled_reminders = {}


def validate_twilio_request():
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("[DEBUG] Twilio credentials missing; skipping request validation")
        return True

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    # Twilio signs with HTTPS, but ngrok forwards as HTTP
    url = request.url.replace("http://", "https://", 1)
    params = request.form.to_dict(flat=True)
    signature = request.headers.get("X-Twilio-Signature", "")
    is_valid = validator.validate(url, params, signature)
    if not is_valid:
        print("[WARNING] Invalid Twilio signature")
        print(f"[DEBUG] URL={url}")
        print(f"[DEBUG] Signature={signature}")
        print(f"[DEBUG] Params keys={list(params.keys())}")
    return is_valid


def get_weather(city):
    if not WEATHER_API_KEY:
        return "Weather API key not configured!"

    city_clean = city.strip().strip("'\".?")
    current_url = (
        f"https://api.openweathermap.org/data/2.5/weather?q={city_clean}&appid={WEATHER_API_KEY}&units=metric"
    )
    try:
        current_resp = requests.get(current_url, timeout=10)
        current_data = current_resp.json()

        if not current_resp.ok or "main" not in current_data:
            return "City not found or weather service unavailable. Please check the city name."

        coord = current_data.get("coord", {})
        lat = coord.get("lat")
        lon = coord.get("lon")
        temp = current_data["main"].get("temp")
        feels_like = current_data["main"].get("feels_like")
        humidity = current_data["main"].get("humidity")
        wind_speed = current_data.get("wind", {}).get("speed")
        description = current_data["weather"][0].get("description", "")

        forecast_text = ""
        if lat is not None and lon is not None:
            onecall_url = (
                f"https://api.openweathermap.org/data/2.5/onecall?lat={lat}&lon={lon}"
                f"&exclude=minutely,hourly,alerts&units=metric&appid={WEATHER_API_KEY}"
            )
            onecall_resp = requests.get(onecall_url, timeout=10)
            onecall_data = onecall_resp.json()
            today = onecall_data.get("daily", [])[0] if onecall_data.get("daily") else None

            if today:
                temp_min = today["temp"].get("min")
                temp_max = today["temp"].get("max")
                pop = int(today.get("pop", 0) * 100)
                forecast_desc = today["weather"][0].get("description", "")
                forecast_text = (
                    f" Today: low {temp_min:.0f}°C, high {temp_max:.0f}°C, "
                    f"{forecast_desc}. Chance of rain {pop}%.")

        wind_text = f", wind {wind_speed} m/s" if wind_speed is not None else ""
        return (
            f"Weather in {city_clean}: {temp:.0f}°C, {description}. "
            f"Feels like {feels_like:.0f}°C, humidity {humidity}%{wind_text}." + forecast_text
        ).replace(" ,", ",")
    except Exception as e:
        return f"Error fetching weather: {str(e)}"


def send_reminder_message(to_number, message_text):
    """Send a reminder message via Twilio WhatsApp"""
    try:
        message = twilio_client.messages.create(
            from_='whatsapp:+14155238886',  # Your Twilio WhatsApp number
            body=message_text,
            to=f'whatsapp:{to_number}'
        )
        print(f"[DEBUG] Reminder sent: {message.sid}")
    except Exception as e:
        print(f"[ERROR] Failed to send reminder: {str(e)}")


def parse_reminder_time(time_str):
    """Parse time string like '14:30' or '2:30 PM' into hour and minute"""
    time_str = time_str.strip().lower()
    try:
        # Try 24-hour format first
        if ':' in time_str:
            parts = time_str.split(':')
            hour = int(parts[0])
            minute = int(parts[1].split()[0]) if ' ' in parts[1] else int(parts[1])
            if 'pm' in time_str and hour != 12:
                hour += 12
            elif 'am' in time_str and hour == 12:
                hour = 0
            return hour, minute
        return None, None
    except:
        return None, None


def parse_datetime_string(datetime_str):
    """Parse date and time string. Returns (datetime_obj, is_specific_date)
    Supports formats like:
    - '14:30' -> recurring daily at 14:30
    - '2025-04-05 14:30' -> specific date
    - 'April 5 14:30' or '5 April 14:30' -> specific date (current year)
    """
    datetime_str = datetime_str.strip()
    current_year = datetime.now().year
    
    # Try parsing with date formats first
    date_formats = [
        ('%Y-%m-%d %H:%M', True),  # 2025-04-05 14:30
        ('%m-%d %H:%M', True),      # 04-05 14:30
        ('%d-%m %H:%M', True),      # 05-04 14:30
        ('%B %d %H:%M', True),      # April 5 14:30
        ('%d %B %H:%M', True),      # 5 April 14:30
        ('%b %d %H:%M', True),      # Apr 5 14:30
        ('%d %b %H:%M', True),      # 5 Apr 14:30
    ]
    
    for fmt, is_specific in date_formats:
        try:
            dt = datetime.strptime(datetime_str, fmt)
            if not fmt.startswith('%Y'):  # Add current year if not in format
                dt = dt.replace(year=current_year)
            # Check if date is in the past
            if dt < datetime.now():
                return None, None, "The date and time are in the past. Please choose a future date."
            return dt, is_specific, None
        except ValueError:
            continue
    
    # Try just time format (recurring)
    try:
        hour, minute = parse_reminder_time(datetime_str)
        if hour is not None and minute is not None:
            return (hour, minute), False, None
        return None, None, "Invalid date/time format. Use HH:MM or YYYY-MM-DD HH:MM"
    except:
        return None, None, "Invalid date/time format"


def schedule_reminder(user_number, reminder_datetime, reminder_message):
    """Schedule a reminder for the user (recurring or specific date)"""
    parsed, is_specific_date, error = parse_datetime_string(reminder_datetime)
    
    if error:
        return error
    
    if parsed is None:
        return "Invalid date/time format. Use HH:MM or YYYY-MM-DD HH:MM"

    if is_specific_date:
        # Specific date and time (one-time)
        dt = parsed
        job_id = f"{user_number}_{dt.strftime('%Y%m%d_%H%M')}"
        
        # Remove existing job if any
        if job_id in scheduled_reminders:
            scheduler.remove_job(job_id)
            del scheduled_reminders[job_id]
        
        # Schedule one-time job
        trigger = DateTrigger(run_date=dt)
        scheduler.add_job(
            send_reminder_message,
            trigger=trigger,
            args=[user_number, reminder_message],
            id=job_id
        )
        
        scheduled_reminders[job_id] = {
            'datetime': dt.strftime('%Y-%m-%d %H:%M'),
            'message': reminder_message,
            'user': user_number,
            'recurring': False
        }
        
        return f"Reminder set for {dt.strftime('%Y-%m-%d at %H:%M')}: {reminder_message}"
    else:
        # Time only (recurring daily)
        hour, minute = parsed
        job_id = f"{user_number}_{hour}_{minute}_daily"
        
        # Remove existing job if any
        if job_id in scheduled_reminders:
            scheduler.remove_job(job_id)
            del scheduled_reminders[job_id]
        
        # Schedule daily job
        trigger = CronTrigger(hour=hour, minute=minute)
        scheduler.add_job(
            send_reminder_message,
            trigger=trigger,
            args=[user_number, reminder_message],
            id=job_id
        )
        
        scheduled_reminders[job_id] = {
            'time': f"{hour:02d}:{minute:02d}",
            'message': reminder_message,
            'user': user_number,
            'recurring': True
        }
        
        return f"Daily reminder set for {hour:02d}:{minute:02d}: {reminder_message}"


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    if not validate_twilio_request():
        abort(403)

    incoming_msg = request.values.get("Body", "").strip()
    user_number = request.values.get("From", "").replace("whatsapp:+", "")
    print(f"[DEBUG] Received message: {incoming_msg}")
    resp = MessagingResponse()
    msg = resp.message()

    # Check for reminder commands first
    # Matches: "Remind me at 14:30 to do X" or "Remind me on 2025-04-05 at 14:30 to do X"
    reminder_match = re.search(
        r"(?:remind me|set reminder|alert me)\s+(?:on\s+(\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}|(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))\s+)?(?:at\s+)?(\d{1,2}:\d{2}(?:\s*[ap]m)?)\s+(?:to\s+)?(.+)",
        incoming_msg,
        re.IGNORECASE
    )
    if reminder_match:
        date_part = reminder_match.group(1) if reminder_match.group(1) else ""
        time_part = reminder_match.group(2).strip()
        reminder_message = reminder_match.group(3).strip()
        
        # Combine date and time if date is present
        if date_part:
            reminder_datetime = f"{date_part} {time_part}"
        else:
            reminder_datetime = time_part
        
        result = schedule_reminder(user_number, reminder_datetime, reminder_message)
        msg.body(result)
    else:
        # Check for weather queries
        patterns = [
            r"\bweather\s+in\s+(.+)",
            r"\btemperature\s+in\s+(.+)",
            r"\btemp\s+in\s+(.+)".replace(" ,", ","),
            r"\bforecast\s+for\s+(.+)",
            r"\bwhat(?:'s| is)\s+the\s+weather\s+in\s+(.+)"
        ]

        city = None
        for pattern in patterns:
            match = re.search(pattern, incoming_msg, re.IGNORECASE)
            if match:
                city = match.group(1).strip().strip("?.!")
                break

        if city:
            print(f"[DEBUG] City extracted: {city}")
            weather_response = get_weather(city)
            print(f"[DEBUG] Weather response: {weather_response}")
            msg.body(weather_response)
        else:
            help_text = ("Hi! I can help with:\n\n"
                        "📍 Weather: 'Weather in London'\n"
                        "🔔 Daily reminder: 'Remind me at 14:30 to take medicine'\n"
                        "📅 One-time reminder: 'Remind me on 2025-04-05 at 14:30 to call mom'\n"
                        "📅 Also try: 'Remind me on April 5 at 2:30 PM to start project'")
            msg.body(help_text)

    print(f"[DEBUG] Response: {str(resp)}")
    return str(resp)


if __name__ == "__main__":
    try:
        app.run(port=5000)
    finally:
        scheduler.shutdown()

