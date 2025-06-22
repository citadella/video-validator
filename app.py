from flask import Flask, render_template, request, redirect, url_for, jsonify
import sqlite3
import os
import subprocess
import shutil
from datetime import datetime
import logging
import threading
import time

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Configure logging for debugging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
app.logger.setLevel(logging.INFO)

DATABASE_PATH = '/app/data/video_validation.db'
MEDIA_PATHS = {
    'movie': '/media/movies',
    'tv': '/media/tv'
}
VIDEO_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.wmv']

CHECKPOINTS = {
    'movie': [60, 600, 1800],   # 1, 10, 30 minutes
    'tv': [60, 300, 600]        # 1, 5, 10 minutes
}

repair_progress = {
    'active': False,
    'current_file': '',
    'completed': 0,
    'total': 0,
    'status': 'idle'
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

def repair_video_file(filepath):
    """Attempt to repair a corrupted video file using multiple FFmpeg strategies.
    If all repair strategies fail, delete the backup."""
    app.logger.info(f"Starting repair for: {filepath}")

    if not os.path.exists(filepath):
        app.logger.error(f"File not found: {filepath}")
        return "failed", "File not found"

    if not os.access(filepath, os.R_OK):
        app.logger.error(f"Cannot read file: {filepath}")
        return "failed", "Cannot read file"

    dir_path = os.path.dirname(filepath)
    if not os.access(dir_path, os.W_OK):
        app.logger.error(f"Cannot write to directory: {dir_path}")
        return "failed", "Cannot write to directory"

    backup_dir = os.path.join(dir_path, '.video_backups')
    filename = os.path.basename(filepath)
    backup_path = os.path.join(backup_dir, f"{filename}.backup")

    try:
        os.makedirs(backup_dir, mode=0o755, exist_ok=True)
        app.logger.info(f"Created backup directory: {backup_dir}")

        if not os.path.exists(backup_path):
            shutil.copy2(filepath, backup_path)
            app.logger.info(f"Created backup: {backup_path}")

        # Check FFmpeg
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            app.logger.error(f"FFmpeg not available: {e}")
            return "failed", "FFmpeg not available"

        # Strategy 1: Container rebuild
        temp_path1 = filepath + ".repair1.tmp"
        container_cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-i', filepath,
            '-c', 'copy',
            temp_path1
        ]
        app.logger.info(f"Attempting container rebuild: {' '.join(container_cmd)}")
        try:
            subprocess.run(container_cmd, check=True, timeout=300, capture_output=True, text=True)
            if os.path.exists(temp_path1) and os.path.getsize(temp_path1) > 0:
                shutil.move(temp_path1, filepath)
                app.logger.info(f"Repaired using container rebuild: {filepath}")
                return "success", "Container rebuild successful"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if os.path.exists(temp_path1):
                os.remove(temp_path1)

        # Strategy 2: Error recovery
        temp_path2 = filepath + ".repair2.tmp"
        recovery_cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-fflags', 'discardcorrupt',
            '-i', filepath,
            '-c', 'copy',
            temp_path2
        ]
        app.logger.info(f"Attempting error recovery: {' '.join(recovery_cmd)}")
        try:
            subprocess.run(recovery_cmd, check=True, timeout=600, capture_output=True, text=True)
            if os.path.exists(temp_path2) and os.path.getsize(temp_path2) > 0:
                shutil.move(temp_path2, filepath)
                app.logger.info(f"Repaired using error recovery: {filepath}")
                return "success", "Error recovery successful"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if os.path.exists(temp_path2):
                os.remove(temp_path2)

        # Strategy 3: Re-encoding
        temp_path3 = filepath + ".repair3.tmp"
        reencode_cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-i', filepath,
            '-c:v', 'libx264', '-crf', '23',
            '-c:a', 'aac',
            '-movflags', '+faststart',
            temp_path3
        ]
        app.logger.info(f"Attempting re-encoding: {' '.join(reencode_cmd)}")
        try:
            subprocess.run(reencode_cmd, check=True, timeout=1800, capture_output=True, text=True)
            if os.path.exists(temp_path3) and os.path.getsize(temp_path3) > 0:
                shutil.move(temp_path3, filepath)
                app.logger.info(f"Repaired using re-encoding: {filepath}")
                return "success", "Re-encoding successful"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if os.path.exists(temp_path3):
                os.remove(temp_path3)

        # If all repair strategies failed, delete the backup
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
                app.logger.info(f"Deleted backup after failed repair: {backup_path}")
            except Exception as e:
                app.logger.error(f"Could not delete backup after failed repair: {e}")
        else:
            app.logger.info(f"No backup found to delete after failed repair: {backup_path}")

        return "failed", "All repair strategies failed"

    except Exception as e:
        app.logger.error(f"Repair exception for {filepath}: {e}")
        # Also delete backup if repair fails due to exception
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
                app.logger.info(f"Deleted backup after failed repair: {backup_path}")
            except Exception as e2:
                app.logger.error(f"Could not delete backup after failed repair: {e2}")
        return "failed", f"Exception: {e}"

def repair_all_failed_files():
    global repair_progress
    with app.app_context():
        try:
            app.logger.info("Repair thread started")
            conn = get_db_connection()
            failed_files = conn.execute(
                'SELECT filepath, media_type FROM validation_results WHERE status = "failed"'
            ).fetchall()
            conn.close()
            repair_progress['total'] = len(failed_files)
            repair_progress['completed'] = 0
            repair_progress['active'] = True
            repair_progress['status'] = 'running'
            app.logger.info(f"Found {len(failed_files)} failed files to repair")
            for file_row in failed_files:
                filepath = file_row['filepath']
                media_type = file_row['media_type']
                repair_progress['current_file'] = os.path.basename(filepath)
                app.logger.info(f"Starting repair for: {filepath}")
                test_dir = os.path.dirname(filepath)
                try:
                    test_file = os.path.join(test_dir, '.write_test')
                    with open(test_file, 'w') as f:
                        f.write('test')
                    os.remove(test_file)
                    app.logger.info(f"Write permissions confirmed for: {test_dir}")
                except Exception as e:
                    app.logger.error(f"Write permission denied for {test_dir}: {e}")
                    continue
                status, message = repair_video_file(filepath)
                app.logger.info(f"Repair result for {filepath}: {status} - {message}")
                if status == "success":
                    validation_status, errors, duration, checks = validate_video(filepath, media_type)
                    conn = get_db_connection()
                    try:
                        file_mtime = os.path.getmtime(filepath)
                        file_size = os.path.getsize(filepath)
                        conn.execute('''
                            UPDATE validation_results 
                            SET status = ?, errors = ?, duration = ?, 
                                file_mtime = ?, file_size = ?, last_checked = ?,
                                check_1m = ?, check_5m = ?, check_10m = ?, check_30m = ?
                            WHERE filepath = ?
                        ''', (
                            validation_status, errors, duration,
                            file_mtime, file_size, datetime.now(),
                            checks.get('check_1m', 0), checks.get('check_5m', 0),
                            checks.get('check_10m', 0), checks.get('check_30m', 0),
                            filepath
                        ))
                        conn.commit()
                        app.logger.info(f"Updated validation for: {filepath} -> {validation_status}")
                    except Exception as e:
                        app.logger.error(f"Database update error: {e}")
                    finally:
                        conn.close()
                repair_progress['completed'] += 1
                time.sleep(0.1)
        except Exception as e:
            app.logger.error(f"Repair thread error: {e}")
            repair_progress['status'] = 'error'
        finally:
            repair_progress['active'] = False
            repair_progress['status'] = 'completed'
            app.logger.info("Repair thread completed")

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
    failed_count = conn.execute(
        'SELECT COUNT(*) FROM validation_results WHERE status = "failed"'
    ).fetchone()[0]
    conn.close()
    return render_template(
        'results.html',
        results=results,
        current_filter=status_filter,
        media_type=media_type,
        page=page,
        total_pages=total_pages,
        total_results=total,
        failed_count=failed_count
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

@app.route('/start-repair', methods=['POST'])
def start_repair():
    global repair_progress
    if repair_progress['active']:
        return jsonify({'error': 'Repair already in progress'}), 400
    app.logger.info("Starting repair thread")
    repair_progress = {
        'active': False,
        'current_file': '',
        'completed': 0,
        'total': 0,
        'status': 'starting'
    }
    repair_thread = threading.Thread(
        target=repair_all_failed_files,
        name="VideoRepair",
        daemon=True
    )
    repair_thread.start()
    return jsonify({'status': 'started', 'message': 'Repair process initiated'})

@app.route('/repair-progress')
def get_repair_progress():
    return jsonify(repair_progress)

@app.route('/debug-repair')
def debug_repair():
    diagnostics = {}
    diagnostics['repair_progress'] = repair_progress
    for media_type, path in MEDIA_PATHS.items():
        try:
            test_file = os.path.join(path, '.write_test')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            diagnostics[f'{media_type}_writable'] = True
        except Exception as e:
            diagnostics[f'{media_type}_writable'] = False
            diagnostics[f'{media_type}_error'] = str(e)
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        diagnostics['ffmpeg_available'] = result.returncode == 0
    except Exception as e:
        diagnostics['ffmpeg_available'] = False
        diagnostics['ffmpeg_error'] = str(e)
    conn = get_db_connection()
    failed_count = conn.execute(
        'SELECT COUNT(*) FROM validation_results WHERE status = "failed"'
    ).fetchone()[0]
    conn.close()
    diagnostics['failed_files_count'] = failed_count
    return jsonify(diagnostics)

@app.route('/settings')
def settings():
    return render_template('settings.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
