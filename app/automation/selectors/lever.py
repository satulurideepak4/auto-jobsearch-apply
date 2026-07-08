"""CSS selectors for Lever ATS application forms."""

SELECTORS: dict = {
    "first_name": "input[name='name']",  # Lever uses a single full-name field
    "last_name": None,  # Combined into the name field above
    "email": "input[name='email']",
    "phone": "input[name='phone']",
    "resume_upload": "input[type='file'][name*='resume']",
    "cover_letter": "textarea[name='comments']",
    "linkedin": "input[name='urls[LinkedIn]']",
    "submit_button": "button[type='submit']",
    "review_button": None,
}
