# Lessons

- Don't order rotation by second-resolution timestamps: rapid sends tie on `sent_at` and starve a message. Order recency by the strictly-increasing send id instead (caught by the fairness test before shipping).
