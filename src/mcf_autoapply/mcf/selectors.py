"""Every MCF page hook the applier relies on, in one place.

Taken from the MCF web app's own element IDs and data-cy test attributes (Oct 2026).
When MCF changes its UI, this is the file to update.
"""

HOME_URL = "https://www.mycareersfuture.gov.sg/"

# Login: "Log In" opens a side panel whose Singpass button goes to login.id.singpass.gov.sg,
# which shows a QR code (an SVG) encoding an https://app.singpass.gov.sg/qrlogin?... link.
SINGPASS_BUTTON = '[data-testid="login-with-singpass"]'
SINGPASS_QR = "#ndi-qr-canvas"
SINGPASS_REFRESH_TEXT = "Refresh QR Code"         # shown when a QR expires (~1 min)

# Applied Jobs page: it loads your applications with a GraphQL query named below, in pages
# of `limit` from `skip`; the response holds applications[].job.uuid, title, employerName,
# statusDescription and submittedOn.
APPLIED_JOBS_URL = HOME_URL + "applied-jobs"
APPLIED_JOBS_QUERY = "getJobApplications"

# Job details page
APPLY_BUTTON = "#job-details-apply-button"        # "Apply", "Applied" or "Apply via Careers@Gov"
LOGIN_TO_APPLY = "#login_button"                  # shown instead when logged out
NAVBAR_LOGIN = "#navbar-login"                    # only when logged out
NAVBAR_SEARCH_JOBS = "#navbar-search-jobs"        # always in the menu bar, once it has loaded
# Note: "Applied Jobs" (#navbar-applied-jobs) lives inside the account drop-down and isn't on
# the page until that menu is opened, so it can't be used to tell whether you're logged in.
NAVBAR_APPLIED_JOBS = "#navbar-applied-jobs"
MCF_ORIGIN = "https://www.mycareersfuture.gov.sg"
APPLIED_TEXT = "Applied"
CAREERS_GOV_TEXT = "Careers@Gov"
CLOSED_TEXTS = ("Applications have closed for this job", "Applications are unavailable till")

# Application flow (route /job/:id/apply)
STEP_HEADER = '[data-cy="job-application__header"]'
STEP_RESUME = "Upload resume"
STEP_SCREENING = "Questions From Employer"
STEP_REVIEW = "Review application"
QUESTION = '[data-cy="screening-questions__question"]'
ANSWER_RADIO = "#answer-{index}-{answer}"         # answer is "yes" or "no"
NEXT_BUTTON_CSS = "button.application-next-step-button, #supporting-details__next-button"
NEXT_BUTTON_NAME = r"^\s*Next\b"                  # "Next: Upload resume", "Next: Review application"…
SUBMIT_BUTTON = "#job-application-review__submit-button"
SUCCESS = '[data-cy="submission-success-text"]'   # "Your application is on its way!"
NOT_PERMITTED = "#job-not-permitted-page"         # job closed, or already applied
ERROR_BANNER = "#error-banner"
FIELD_ERRORS = ("Please fill this in", "Please select an option")

# The GraphQL mutation the page sends on Submit; its response carries the application ID.
CREATE_APPLICATION_MUTATION = "createApplication"

CAPTCHA = 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"]'
