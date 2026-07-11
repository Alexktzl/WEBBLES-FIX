def get_user(cursor, uid):
    cursor.execute("SELECT * FROM users WHERE id = ?", (uid,))
    return cursor.fetchone()
