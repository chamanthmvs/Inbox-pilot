# Load backend/.env (if present) as early as possible, so ANTHROPIC_API_KEY,
# CLASSIFIER_MODEL, DRY_RUN, etc. can live in a gitignored file. Runs on any
# `import app.*`, covering the API and the CLIs alike.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass
