"""Every JobStreet page hook the applier relies on, in one place.

JobStreet runs on SEEK's platform. These hooks come from SEEK's standard apply flow, NOT from
inspecting JobStreet while logged in (its pages sit behind a Cloudflare challenge that only a
real browser on a home connection passes). Expect to correct them from the first dry-run
screenshots and the saved .html next to them. Each entry lists alternatives, tried in order.
"""

HOME_URL = "https://sg.jobstreet.com/"
ORIGIN = "https://sg.jobstreet.com"
LOGIN_URL = "https://sg.jobstreet.com/oauth/login/"
APPLIED_JOBS_URL = "https://sg.jobstreet.com/my-activity/applied-jobs"

# Cloudflare's interstitial. It usually clears by itself in a real browser within seconds.
CLOUDFLARE_TITLE = "Just a moment"
CLOUDFLARE_CLEAR_S = 30

# Logged in / out (header)
SIGN_IN_LINK = 'a[data-automation="sign in"], a[data-automation="signinLink"], a[href*="/oauth/login"], a[href*="login"]'
SIGN_IN_TEXT = r"^\s*Sign in\s*$"
ACCOUNT_MENU = ('[data-automation="profile-menu"], [data-automation="profileMenu"], '
                'a[href*="/my-activity"], a[href*="/profile"]')

# Job details page. Tried in this order (a CSS list would match in page order instead, and a
# "How to apply" article link could win).
APPLY_BUTTONS = ('[data-automation="job-detail-apply"]', 'a[data-automation*="apply" i]',
                 'a:has-text("Quick apply")', 'button:has-text("Quick apply")')
APPLY_BUTTON = ", ".join(APPLY_BUTTONS)     # any of them (for waiting on the page to load)
QUICK_APPLY_TEXT = "Quick apply"
# SEEK's own form lives at /job/<id>/apply; a link anywhere else is the employer's site.
ONSITE_APPLY_PATH = r"/job/[^/?#]+/apply"
APPLIED_MARKERS = ("You applied for this job", "You've applied", "Applied on", "already applied")
CLOSED_MARKERS = ("This job is no longer advertised", "no longer accepting", "job has expired",
                  "This job is no longer available")

# Apply flow (sg.jobstreet.com/job/<id>/apply/...)
STEP_HEADING = "main h1, main h2, [data-automation='apply-header'], form h1, form h2"
# Steps are told apart by their controls (below); headings only appear in messages.

SAVED_RESUME_RADIO = ('[data-testid="resume-method-change"], input[value="change"], '
                      'label:has-text("Select a resum"), label:has-text("Choose a resum")')
SAVED_RESUME_TEXT = r"(select|choose) a r[eé]sum[eé]"
RESUME_SELECT = 'select[data-testid="select-input"], select[name*="resume"], select'
COVER_LETTER_NONE = ('[data-testid="coverLetter-method-none"], input[name*="cover" i][value="none"], '
                     'label:has-text("Don\'t include a cover letter")')
COVER_LETTER_NONE_TEXT = r"don.?t include a cover letter"
# The documents step's own choices; recognises that step (a dropdown alone doesn't: question
# steps have dropdowns too).
DOCUMENT_CHOICE_TEXT = r"(upload|select|choose|don.?t include|write) a (r[eé]sum[eé]|cover letter)"
# Words that mark the documents step's own radio groups (never employer questions).
DOCUMENT_GROUP_WORDS = ("resum", "cover letter", "selection criteria")
# Step addresses inside /job/<id>/apply (SEEK); used as hints alongside the page's controls.
QUESTIONS_URL_PARTS = ("role-requirements", "questions")
PROFILE_URL_PARTS = ("/profile",)
CONTINUE_BUTTON = ('[data-testid="continue-button"], button[data-automation="continue"], '
                   'button:has-text("Continue")')
SUBMIT_BUTTON = ('[data-testid="review-submit-application"], button[data-automation="submit-application"], '
                 'button:has-text("Submit application")')
SUCCESS_URL_PART = "/apply/success"
SUCCESS_TEXTS = ("application has been submitted", "Application sent", "You've applied")
FIELD_ERRORS = ("is required", "Please select", "Please enter", "This field is required")

# Employer questions: each question is a group with a label/legend and radio, checkbox,
# select or text inputs. We read them generically rather than by a fixed hook.
QUESTION_GROUPS = 'fieldset, [role="group"], [data-testid^="question"], [data-automation^="question"]'
