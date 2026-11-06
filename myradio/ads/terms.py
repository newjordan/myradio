"""The two confirmations an operator makes before spoken or paid spots may be used.

The wording lives here so that the screen, the tests and the contract document agree. It is shown
only by ``GET /acknowledge``; it is never part of the station status. A confirmation is stored with
a timestamp and the wording version, and it is set only through the authenticated HTTP route
``POST /acknowledge``. There is deliberately no operation for it, so a chat model or an outside
agent cannot tick a legal box for the operator.

This is a plain-language reminder to check the licences. It is not legal advice.
"""
from __future__ import annotations

# Raise this when the wording changes in a way that needs a fresh confirmation.
TERMS_VERSION = 1

KINDS = ("voice_rights", "commercial_use")

TERMS = {
    "voice_rights": {
        "title": "Voice rights",
        "text": ("I have the right to use this station's voice for spoken messages. I have read the voice "
                 "model's license. I understand it does not allow offering a hosted voice service to other "
                 "people, and it does not allow copying a real person's voice without their consent."),
    },
    "commercial_use": {
        "title": "Paid spots",
        "text": ("I understand that playing paid sponsor messages is commercial use. I have checked the licenses "
                 "of the music and voice models this station uses, including that the music model's card marks "
                 "the music it makes as CC BY-NC 4.0 (non-commercial). I am responsible for making sure my use "
                 "is allowed."),
    },
}

NEEDS_VOICE_RIGHTS = ("Spoken messages stay on hold until you confirm you have the right to use the voice. "
                      "Open Spots, read the voice note and confirm it, or use a recorded spot instead.")
NEEDS_COMMERCIAL_USE = ("Paid spots need one confirmation first: that you have checked the licenses and are allowed "
                        "to run them. Open Spots, read the note and confirm it.")
