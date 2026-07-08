"""CSS selectors for iCIMS ATS application forms."""

SELECTORS: dict = {
    "first_name": "input[id*='firstname'], input[name*='fname'], input[name*='first']",
    "last_name":  "input[id*='lastname'],  input[name*='lname'], input[name*='last']",
    "email":      "input[id*='email'], input[type='email']",
    "phone":      "input[id*='phone'], input[id*='mobile'], input[type='tel']",
    "resume_upload": "input[type='file']",
    "cover_letter":  "textarea[id*='cover'], textarea[name*='cover']",
    "linkedin":      "input[id*='linkedin'], input[placeholder*='LinkedIn']",
    "submit_button": "input[type='submit'], button[type='submit']",
    "review_button": None,
}
