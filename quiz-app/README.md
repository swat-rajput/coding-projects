# Quiz App

Flask + SQLite quiz. Each quiz serves 20 random questions from `questions_rebalanced.json`.
Answered questions are recorded in `quiz.db` and are not served again until you click **Reset progress**.

```
python3 app.py
```

Then open http://127.0.0.1:5050. Questions are (re)loaded from the JSON into `quiz.db` on every start,
so edits to the JSON are picked up without losing progress.
