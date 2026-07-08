"""CSS selectors for Greenhouse ATS application forms."""

SELECTORS: dict = {
    "first_name": "#first_name",
    "last_name": "#last_name",
    "email": "#email",
    "phone": "#phone",
    "resume_upload": "input[type='file']",
    "cover_letter": "#cover_letter",
    "linkedin": "input[name*='linkedin']",
    "submit_button": "#submit_app",
    "review_button": "input[value*='Review']",
}
