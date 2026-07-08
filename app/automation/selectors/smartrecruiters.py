"""CSS selectors for SmartRecruiters ATS application forms."""

SELECTORS: dict = {
    "first_name": "input[name='firstName'], input[id*='firstName'], input[placeholder*='First']",
    "last_name":  "input[name='lastName'],  input[id*='lastName'],  input[placeholder*='Last']",
    "email":      "input[name='email'],      input[type='email']",
    "phone":      "input[name='phoneNumber'], input[name='phone'], input[type='tel']",
    "resume_upload": "input[type='file']",
    "cover_letter":  "textarea[name*='message'], textarea[name*='cover'], textarea[id*='cover']",
    "linkedin":      "input[name*='linkedin'], input[placeholder*='LinkedIn']",
    "submit_button": "button[data-ui='submit-btn'], button[type='submit'], button:has-text('Submit')",
    "review_button": None,
}
