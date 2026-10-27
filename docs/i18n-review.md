# Language review checklist

`shared/agricommon/i18n.py` holds English, Bemba (`bem`) and Nyanja (`nya`) strings. The Bemba and Nyanja strings are **drafts**: only the main menu and a few prompts are translated, and every other key falls back to English.

Before any field pilot, a native speaker should, for each language:

1. Review every existing string for meaning and natural phrasing.
2. Translate the remaining keys (copy the `en` block).
3. Run `pytest tests/unit/test_shared.py`. Every screen must stay within 182 characters and the GSM-7 alphabet, because characters outside it double the SMS cost.
