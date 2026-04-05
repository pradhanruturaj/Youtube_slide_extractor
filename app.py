# app.py

import os
import cv2
import subprocess
import tempfile
from flask import Flask, render_template, request, send_file, jsonify
from img2pdf import convert
import threading
import time
import re

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1GB max file size
app.config['UPLOAD_FOLDER'] = tempfile.gettempdir()

# Store job status
jobs = {}

def get_duration_seconds(duration_str):
    """Convert duration string to seconds"""
    parts = duration_str.split(':')
    if len(parts) == 2:
        return int(parts[0])*60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
    else:
        return int(duration_str)

def extract_slides_job(job_id, youtube_url, interval_seconds, quality=95):
    """Background job to extract slides and create PDF"""
    try:
        jobs[job_id]['status'] = 'downloading'
        jobs[job_id]['message'] = 'Downloading video...'
        
        # Create temp directory for this job
        temp_dir = os.path.join(tempfile.gettempdir(), job_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # Download video
        video_path = os.path.join(temp_dir, 'video.mp4')
        subprocess.run([
            'yt-dlp', '-f', 'best[ext=mp4]', 
            '-o', video_path, 
            youtube_url
        ], check=True, capture_output=True, text=True)
        
        jobs[job_id]['message'] = 'Getting video duration...'
        
        # Get video duration
        result = subprocess.run(
            ['yt-dlp', '--get-duration', youtube_url],
            capture_output=True, text=True
        )
        duration_str = result.stdout.strip()
        duration = get_duration_seconds(duration_str)
        
        jobs[job_id]['duration'] = duration
        jobs[job_id]['message'] = f'Extracting frames every {interval_seconds} seconds...'
        jobs[job_id]['status'] = 'extracting'
        
        # Extract frames
        cap = cv2.VideoCapture(video_path)
        images = []
        total_frames = duration // interval_seconds + 1
        frame_count = 0
        
        for time_sec in range(0, duration, interval_seconds):
            cap.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000)
            ret, frame = cap.read()
            
            if ret:
                minutes = time_sec // 60
                seconds = time_sec % 60
                img_path = os.path.join(temp_dir, f'slide_{minutes:02d}_{seconds:02d}.jpg')
                cv2.imwrite(img_path, frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                images.append(img_path)
                frame_count += 1
                jobs[job_id]['progress'] = (frame_count / total_frames) * 100
                jobs[job_id]['message'] = f'Extracted {frame_count} slides...'
        
        cap.release()
        
        if images:
            jobs[job_id]['message'] = f'Creating PDF with {len(images)} slides...'
            jobs[job_id]['status'] = 'creating_pdf'
            
            # Create PDF
            pdf_path = os.path.join(temp_dir, 'slides.pdf')
            with open(pdf_path, 'wb') as f:
                f.write(convert(images))
            
            jobs[job_id]['pdf_path'] = pdf_path
            jobs[job_id]['slide_count'] = len(images)
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['message'] = 'PDF created successfully!'
            jobs[job_id]['progress'] = 100
        else:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['message'] = 'No slides extracted'
            
    except subprocess.CalledProcessError as e:
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['message'] = f'Error downloading video: {str(e)}'
    except Exception as e:
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['message'] = f'Error: {str(e)}'

@app.route('/')
def index():
    """Render the main page"""
    return render_template('index.html')

@app.route('/extract', methods=['POST'])
def extract():
    """Start extraction job"""
    youtube_url = request.form.get('youtube_url')
    interval_seconds = int(request.form.get('interval_seconds', 10))
    quality = int(request.form.get('quality', 95))
    
    if not youtube_url:
        return jsonify({'error': 'YouTube URL is required'}), 400
    
    # Validate URL
    youtube_regex = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/'
    if not re.match(youtube_regex, youtube_url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    # Create job
    job_id = str(int(time.time()))
    jobs[job_id] = {
        'status': 'pending',
        'message': 'Starting extraction...',
        'progress': 0,
        'youtube_url': youtube_url,
        'interval_seconds': interval_seconds,
        'quality': quality
    }
    
    # Start background thread
    thread = threading.Thread(
        target=extract_slides_job,
        args=(job_id, youtube_url, interval_seconds, quality)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def status(job_id):
    """Get job status"""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    response = {
        'status': job['status'],
        'message': job['message'],
        'progress': job.get('progress', 0)
    }
    
    if job['status'] == 'completed':
        response['slide_count'] = job.get('slide_count', 0)
        response['download_url'] = f"/download/{job_id}"
    
    return jsonify(response)

@app.route('/download/<job_id>')
def download(job_id):
    """Download the generated PDF"""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    if job['status'] != 'completed':
        return jsonify({'error': 'PDF not ready yet'}), 400
    
    pdf_path = job.get('pdf_path')
    if not pdf_path or not os.path.exists(pdf_path):
        return jsonify({'error': 'PDF file not found'}), 404
    
    return send_file(
        pdf_path,
        as_attachment=True,
        download_name='slides.pdf',
        mimetype='application/pdf'
    )

@app.route('/cleanup/<job_id>', methods=['DELETE'])
def cleanup(job_id):
    """Clean up job files"""
    if job_id in jobs:
        temp_dir = os.path.join(tempfile.gettempdir(), job_id)
        if os.path.exists(temp_dir):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        del jobs[job_id]
        return jsonify({'message': 'Cleanup successful'})
    return jsonify({'error': 'Job not found'}), 404

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)