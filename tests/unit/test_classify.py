import pytest

from jobpilot.forms.classify import classify_sensitive

POSITIVES = [
    # authorization
    (
        "Will you now or in the future require sponsorship for employment visa status?",
        "authorization",
    ),
    ("Do you require visa sponsorship?", "authorization"),
    ("Are you legally authorized to work in the United States?", "authorization"),
    ("Are you authorised to work in the UK?", "authorization"),
    ("Are you eligible to work in India?", "authorization"),
    ("Are you legally permitted to work in Canada?", "authorization"),
    ("Are you legally able to work in Germany?", "authorization"),
    ("Do you have the right to work in the EU?", "authorization"),
    ("Work permit status", "authorization"),
    ("Do you hold an H-1B visa?", "authorization"),
    ("Current H1B status", "authorization"),
    ("Citizenship", "authorization"),
    ("Are you a U.S. citizen?", "authorization"),
    ("Nationality *", "authorization"),
    ("Employment visa sponsored by a previous employer?", "authorization"),
    ("Work authorization status (required)", "authorization"),
    # demographic
    ("How do you identify?", "demographic"),
    ("Gender", "demographic"),
    ("What is your sex?", "demographic"),
    ("Preferred pronouns", "demographic"),
    ("Race/Ethnicity", "demographic"),
    ("Are you Hispanic or Latino?", "demographic"),
    ("Veteran status", "demographic"),
    ("Protected veteran?", "demographic"),
    ("Do you have a disability?", "demographic"),
    ("Disability status", "demographic"),
    ("Sexual orientation", "demographic"),
    ("Do you identify as LGBTQ+?", "demographic"),
    ("Are you transgender?", "demographic"),
    ("Caste", "demographic"),
    ("Religion", "demographic"),
    ("Marital status", "demographic"),
    ("Are you 18 or older?", "demographic"),
    ("Are you over 18 years of age?", "demographic"),
    ("Age", "demographic"),
    ("Date of birth", "demographic"),
    ("DOB (dd/mm/yyyy)", "demographic"),
    ("Birthday", "demographic"),
    ("Military status", "demographic"),
    # declaration
    ("I certify that the information provided is true and complete", "declaration"),
    ("I agree to the Terms and Conditions", "declaration"),
    ("I acknowledge the Privacy Policy", "declaration"),
    ("I understand that providing false information may result in termination", "declaration"),
    ("I consent to the processing of my data", "declaration"),
    ("Do you consent to a background check?", "declaration"),
    ("I confirm the above is accurate", "declaration"),
    ("I attest that my answers are correct", "declaration"),
    ("Terms of Service", "declaration"),
    # compensation
    ("Expected salary", "compensation"),
    ("Current CTC", "compensation"),
    ("Salary expectations", "compensation"),
    ("What are your pay expectations?", "compensation"),
    ("Desired compensation", "compensation"),
    ("Expected pay range", "compensation"),
    ("Will you require employer support to work in India?", "authorization"),
    ("What is your current immigration status?", "authorization"),
    ("Do you hold a green card?", "authorization"),
    ("Are you a permanent resident?", "authorization"),
    ("Current work status", "authorization"),
    ("Are you a US Person for export control purposes?", "authorization"),
    ("Do you have a criminal record?", "background"),
    ("Have you ever been convicted of a felony?", "background"),
    ("Are you currently employed by a government entity?", "background"),
    ("Do you have any relatives working at the company?", "background"),
    ("Are you related to anyone at Acme?", "background"),
    ("Are you subject to a non-compete agreement?", "background"),
    ("E-signature", "declaration"),
    ("Signature", "declaration"),
    ("Type your full name to sign", "declaration"),
    ("I have read and understood the above", "declaration"),
    ("Do you have any physical or mental impairment?", "demographic"),
    ("Any medical condition we should know about?", "demographic"),
    ("Have you ever been arrested?", "background"),
    ("Do you have any pending charge?", "background"),
    ("Have you faced a criminal charge?", "background"),
    ("Has a family member worked at this company?", "background"),
    ("Do any relatives work for us?", "background"),
    ("Are you a former employee?", "background"),
    ("Were you previously employed by Example Corp?", "background"),
    ("Have you ever been terminated?", "background"),
    ("Have you been dismissed from a job?", "background"),
    ("Have you been asked to resign?", "background"),
    ("Are you bound by a restrictive covenant?", "background"),
    ("Are you a politically exposed person?", "background"),
    ("Please initial", "declaration"),
    ("Initial here", "declaration"),
    ("Date signed", "declaration"),
]

NEGATIVES = [
    "First name",
    "Last name",
    "Email",
    "Phone",
    "Notice period",
    "Years of Python experience",
    "Why do you want to work here?",
    "LinkedIn profile",
    "Are you open to relocation?",
    "Preferred pronunciation of your name",
    "Message to the hiring manager",
    "Language",
    "Current company",
    "Cover letter",
    "How did you hear about us?",
    "Website",
    "Design systems experience",
    "Describe your design process",
]


@pytest.mark.parametrize(("label", "expected"), POSITIVES)
def test_positive_labels(label: str, expected: str) -> None:
    assert classify_sensitive(label) == expected


@pytest.mark.parametrize("label", NEGATIVES)
def test_negative_labels(label: str) -> None:
    assert classify_sensitive(label) is None


def test_help_text_is_searched() -> None:
    assert (
        classify_sensitive("Question 7", help_text="Are you authorized to work in the US?")
        == "authorization"
    )


def test_options_detect_demographic_sets() -> None:
    assert (
        classify_sensitive("Select one", options=["Male", "Female", "Prefer not to say"])
        == "demographic"
    )
    assert (
        classify_sensitive("Choice", options=["I am a protected veteran", "I am not"])
        == "demographic"
    )
    assert classify_sensitive("Choice", options=["Yes", "No"]) is None


def test_precedence_declaration_over_authorization_over_demographic() -> None:
    assert (
        classify_sensitive("I certify I am authorized to work and my gender is accurate")
        == "declaration"
    )
    assert classify_sensitive("Authorized to work? Gender?") == "authorization"
    assert classify_sensitive("Gender and expected salary") == "demographic"


def test_empty_and_none_options() -> None:
    assert classify_sensitive("") is None
    assert classify_sensitive("x", "", None) is None
