"""CSS selectors for Ashby ATS application forms."""

SELECTORS: dict = {
    "first_name": "input[data-testid='input-firstName']",
    "last_name": "input[data-testid='input-lastName']",
    "email": "input[data-testid='input-email']",
    "phone": "input[data-testid='input-phone']",
    "resume_upload": "input[type='file']",
    "cover_letter": "textarea[data-testid*='cover']",
    "linkedin": "input[placeholder*='LinkedIn']",
    "submit_button": "button[data-testid='submit-application-button']",
    "review_button": None,
}
