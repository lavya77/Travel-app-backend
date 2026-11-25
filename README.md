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

## Test account

Set `SEED_TEST_USER_USERNAME`, `SEED_TEST_USER_EMAIL`, and `SEED_TEST_USER_PASSWORD` in the ignored `.env` file, then run:

```bash
python manage.py seed_test_user
```

The command is idempotent, requires a password of at least 8 characters, and never prints the password. Keep the test password in `.env`; do not commit it.
