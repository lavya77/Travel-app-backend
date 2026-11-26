# Smart Travel Planner API

Django REST Framework API for the Smart Travel Planner frontend. PostgreSQL is required.

## Local setup

Create and activate the backend Conda environment, then install dependencies:

```bash
conda create -n smarttravel_backend python=3.12 pip
conda activate smarttravel_backend
pip install -r requirements.txt
```

Create a local `.env` from `.env.example`, replace the placeholder secrets, and set the PostgreSQL database name, user and password. Never commit `.env`.

Run migrations, tests and the API server from this directory:

```bash
python manage.py migrate
python manage.py test
python manage.py runserver 127.0.0.1:8000
```

The API is then available at `http://127.0.0.1:8000/api/`. JWT keys, database settings, allowed hosts, CORS origins, and Django debug mode are configured through environment variables.

## Optional AI itinerary planning

AI is off unless a traveller opts in for that plan generation. To enable it,
set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in the ignored backend
`.env` file. Create a Cloudflare API token with the Account > Workers AI >
Read permission, scoped to the account that owns the model; the token must
never be exposed in frontend code. The default model is `@cf/qwen/qwen3.8-27b`.
The selected provider receives the trip route, dates, traveller count, and
saved sightseeing place names, categories, visit durations, and coordinates.
Cloudflare lists zero data retention for this model. Workers AI's Free plan
includes 10,000 Neurons per day; requests beyond this fail on Free, while the
Paid plan bills usage above the free allocation. Failed requests use the
existing rule-based planner and add a warning. The backend validates returned
place IDs and enforces its own travel-time and scheduling rules. Never commit
API tokens.

## Test account

Set `SEED_TEST_USER_USERNAME`, `SEED_TEST_USER_EMAIL`, and `SEED_TEST_USER_PASSWORD` in the ignored `.env` file, then run:

```bash
python manage.py seed_test_user
```

The command is idempotent, requires a password of at least 8 characters, and never prints the password. Keep the test password in `.env`; do not commit it.
