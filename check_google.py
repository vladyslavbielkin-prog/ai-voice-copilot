from google.oauth2 import service_account
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech
from google.api_core.client_options import ClientOptions

creds = service_account.Credentials.from_service_account_file(
    "silver-tape-489309-g1-feb424b62a17.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
)
client = SpeechClient(
    credentials=creds,
    client_options=ClientOptions(api_endpoint="us-central1-speech.googleapis.com"),
)
try:
    req = cloud_speech.GetRecognizerRequest(
        name="projects/silver-tape-489309-g1/locations/us-central1/recognizers/_"
    )
    client.get_recognizer(request=req)
    print("OK API enabled")
except Exception as e:
    msg = str(e)
    if "has not been used" in msg or "disabled" in msg or "SERVICE_DISABLED" in msg:
        print("FAIL API not enabled")
        print(msg[:300])
    elif "NOT_FOUND" in msg or "not found" in msg.lower():
        print("OK API enabled (recognizer _ not found - normal)")
    else:
        print("OTHER: " + msg[:300])
