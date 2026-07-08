"""CSS selectors for Workday ATS application forms."""

SELECTORS: dict = {
    "first_name": "input[data-automation-id='legalNameSection_firstName']",
    "last_name": "input[data-automation-id='legalNameSection_lastName']",
    "email": "input[data-automation-id='email']",
    "phone": "input[data-automation-id='phone-number']",
    "resume_upload": "input[type='file']",
    "cover_letter": "textarea[data-automation-id*='coverLetter']",
    "linkedin": "input[aria-label*='LinkedIn']",
    "submit_button": "button[data-automation-id='bottom-navigation-next-button']",
    "review_button": "button[data-automation-id='bottom-navigation-review-button']",
}
