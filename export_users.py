import sqlite3
import csv

conn = sqlite3.connect('diesel.db')
cursor = conn.cursor()

cursor.execute('SELECT user_id, controller_id FROM user_controllers')
rows = cursor.fetchall()

print(f'📊 Найдено {len(rows)} записей в user_controllers')

with open('user_controllers_clean.csv', 'w', encoding='utf-8', newline='') as f:
    writer = csv.writer(f, delimiter=',', quotechar='"')
    writer.writerow(['user_id', 'controller_id'])
    for row in rows:
        clean_row = []
        for val in row:
            if val is None:
                clean_row.append('')
            else:
                clean_row.append(str(val))
        writer.writerow(clean_row)

conn.close()
print('✅ user_controllers_clean.csv создан')
