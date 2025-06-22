from flask import Flask, render_template, request, redirect, url_for
import sqlite3
import os
import subprocess
from datetime import datetime
import logging

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'
app.logger.setLevel(logging.INFO)

DATABASE_PATH = '/app/data/video_validation.db'
MEDIA_PATHS = {
    'movie': '/media/movies',
    'tv': '/media/tv'
}
VIDEO_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.wmv']

CHECKPOINTS = {
    'movie': [60, 600, 1800],
    'tv': [60, 300, 600]
}

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS validation_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT NOT NULL,
            filepath TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            errors TEXT,
            check_1m INTEGER DEFAULT 0,
            check_5m INTEGER DEFAULT 0,
            check_10m INTEGER DEFAULT 0,
            check_30m INTEGER DEFAULT 0,
            duration REAL,
            file_mtime REAL,
            file_size INTEGER,
            last_checked TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def validate_video(filepath, media_type):
    checkpoints = CHECKPOINTS[media_type]
    errors = []
    check_results = {f'check_{t//60}m': 0 for t in [60, 300, 600, 1800]}

    try:
        duration_cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', filepath
        ]
        duration = float(subprocess.check_output(duration_cmd).decode().strip())
    except Exception as e:
        return 'failed', f'[{filepath}] Could not get duration: {e}', 0, check_results

    for seconds in checkpoints:
        checkpoint_name = f'check_{seconds//60}m'
        if duration > seconds:
            ffmpeg_cmd = [
                'ffmpeg',
                '-v', 'error',
                '-ss', str(seconds), '-t', '1',
                '-i', filepath, '-f', 'null', '-'
            ]
            try:
                subprocess.check_output(ffmpeg_cmd, stderr=subprocess.STDOUT, timeout=15)
                check_results[checkpoint_name] = 1
            except subprocess.CalledProcessError as e:
                error_msg = f'[{filepath}] {checkpoint_name}: {e.output.decode().strip()}'
                errors.append(error_msg)
                check_results[checkpoint_name] = 0
            except subprocess.TimeoutExpired:
                error_msg = f'[{filepath}] {checkpoint_name}: Timeout'
                errors.append(error_msg)
                check_results[checkpoint_name] = 0
        else:
            check_results[checkpoint_name] = -1

    status = "passed" if not errors else "failed"
    return status, '\n'.join(errors), duration, check_results

def should_validate_file(filepath, db_row):
    if db_row is None or db_row['status'] == 'failed':
        return True
    try:
        current_mtime = os.path.getmtime(filepath)
        current_size = os.path.getsize(filepath)
        if (current_mtime != db_row['file_mtime'] or 
            current_size != db_row['file_size']):
            return True
    except Exception:
        pass
    return False

@app.route('/results')
def results():
    media_type = request.args.get('media', 'movie')
    status_filter = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)
    per_page = 200
    offset = (page - 1) * per_page

    conn = get_db_connection()
    
    base_query = 'SELECT * FROM validation_results WHERE media_type = ?'
    count_query = 'SELECT COUNT(*) FROM validation_results WHERE media_type = ?'
    
    if status_filter == 'failed':
        base_query += ' AND status = "failed"'
        count_query += ' AND status = "failed"'

    total = conn.execute(count_query, (media_type,)).fetchone()[0]
    total_pages = (total + per_page - 1) // per_page

    results = conn.execute(
        f'{base_query} ORDER BY last_checked DESC LIMIT ? OFFSET ?',
        (media_type, per_page, offset)
    ).fetchall()
    
    conn.close()

    return render_template(
        'results.html',
        results=results,
        current_filter=status_filter,
        media_type=media_type,
        page=page,
        total_pages=total_pages,
        total_results=total
    )

@app.route('/')
def dashboard():
    conn = get_db_connection()
    stats = {}
    for media_type in MEDIA_PATHS:
        media_stats = conn.execute('''
            SELECT 
                COUNT(*) AS total_files,
                SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS passed_files,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_files,
                SUM(CASE WHEN check_1m = 0 THEN 1 ELSE 0 END) AS failed_1m,
                SUM(CASE WHEN check_5m = 0 THEN 1 ELSE 0 END) AS failed_5m,
                SUM(CASE WHEN check_10m = 0 THEN 1 ELSE 0 END) AS failed_10m,
                SUM(CASE WHEN check_30m = 0 THEN 1 ELSE 0 END) AS failed_30m
            FROM validation_results
            WHERE media_type = ?
        ''', (media_type,)).fetchone()
        stats[media_type] = {
            'total': media_stats['total_files'] or 0,
            'passed': media_stats['passed_files'] or 0,
            'failed': media_stats['failed_files'] or 0,
            'checkpoints': {
                '1m': media_stats['failed_1m'] or 0,
                '5m': media_stats['failed_5m'] or 0,
                '10m': media_stats['failed_10m'] or 0,
                '30m': media_stats['failed_30m'] or 0
            }
        }
    conn.close()
    return render_template('dashboard.html', stats=stats)

@app.route('/start-scan', methods=['POST'])
def start_scan():
    conn = get_db_connection()
    db_files = {row['filepath']: row for row in conn.execute('SELECT * FROM validation_results')}
    existing_files = set()

    for media_type, base_path in MEDIA_PATHS.items():
        for root, dirs, files in os.walk(base_path):
            for file in files:
                if os.path.splitext(file)[1].lower() in VIDEO_EXTENSIONS:
                    filepath = os.path.join(root, file)
                    existing_files.add(filepath)
                    
                    db_row = db_files.get(filepath)
                    
                    if should_validate_file(filepath, db_row):
                        try:
                            file_mtime = os.path.getmtime(filepath)
                            file_size = os.path.getsize(filepath)
                        except Exception as e:
                            app.logger.error(f"Error accessing {filepath}: {e}")
                            continue
                        
                        status, errors, duration, checks = validate_video(filepath, media_type)
                        conn.execute('''
                            INSERT OR REPLACE INTO validation_results
                            (media_type, filepath, status, errors, duration, 
                             file_mtime, file_size, last_checked,
                             check_1m, check_5m, check_10m, check_30m)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            media_type, filepath, status, errors, duration,
                            file_mtime, file_size, datetime.now(),
                            checks.get('check_1m', 0), checks.get('check_5m', 0),
                            checks.get('check_10m', 0), checks.get('check_30m', 0)
                        ))
                        conn.commit()
                        app.logger.info(f"Revalidated {'FAILED' if status=='failed' else 'PASSED'}: {filepath}")

    # Delete entries for files that no longer exist
    deleted_count = 0
    if existing_files:
        placeholders = ','.join('?' for _ in existing_files)
        cursor = conn.execute(f'''
            DELETE FROM validation_results 
            WHERE filepath NOT IN ({placeholders})
        ''', tuple(existing_files))
        conn.commit()
        deleted_count = cursor.rowcount
    
    app.logger.info(f"Removed {deleted_count} deleted files from database")
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/settings')
def settings():
    return render_template('settings.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
