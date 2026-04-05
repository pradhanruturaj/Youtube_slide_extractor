import os
import cv2
import subprocess
import tempfile
import shutil
import time
import threading
from flask import Flask, render_template, request, send_file, jsonify
from img2pdf import convert

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1GB

# Store job status
jobs = {}

def extract_slides_job(job_id, youtube_url, interval_seconds, quality):
    """Background job to extract slides and create PDF"""
    try:
        # Update status
        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['message'] = 'Downloading video...'
        jobs[job_id]['progress'] = 10
        
        # Create temp directory
        temp_dir = os.path.join(tempfile.gettempdir(), job_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # Download video
        video_path = os.path.join(temp_dir, 'video.mp4')
        result = subprocess.run([
            'yt-dlp', '-f', 'best[ext=mp4]', 
            '-o', video_path, 
            youtube_url
        ], capture_output=True, text=True, timeout=600)
        
        if result.returncode != 0:
            raise Exception(f"Download failed: {result.stderr}")
        
        jobs[job_id]['message'] = 'Extracting frames...'
        jobs[job_id]['progress'] = 30
        
        # Extract frames using OpenCV
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise Exception("Could not open video file")
        
        # Get video properties
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = int(total_frames / fps) if fps > 0 else 0
        
        if duration == 0:
            raise Exception("Could not determine video duration")
        
        images = []
        frame_count = 0
        
        for time_sec in range(0, duration, interval_seconds):
            # Set position in milliseconds
            cap.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000)
            ret, frame = cap.read()
            
            if ret and frame is not None:
                # Save frame as JPEG
                img_path = os.path.join(temp_dir, f'slide_{time_sec}.jpg')
                cv2.imwrite(img_path, frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                images.append(img_path)
                frame_count += 1
                
                # Update progress (30% to 90%)
                progress = 30 + int((time_sec / duration) * 60)
                jobs[job_id]['progress'] = min(progress, 90)
                jobs[job_id]['message'] = f'Extracted {frame_count} slides...'
        
        cap.release()
        
        jobs[job_id]['message'] = 'Creating PDF...'
        jobs[job_id]['progress'] = 95
        
        # Create PDF
        if images:
            pdf_path = os.path.join(temp_dir, 'slides.pdf')
            with open(pdf_path, 'wb') as f:
                f.write(convert(images))
            
            jobs[job_id]['pdf_path'] = pdf_path
            jobs[job_id]['slide_count'] = len(images)
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['message'] = f'PDF ready with {len(images)} slides!'
            jobs[job_id]['progress'] = 100
        else:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['message'] = 'No slides could be extracted'
            
    except subprocess.TimeoutExpired:
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['message'] = 'Download timeout (took too long)'
    except Exception as e:
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['message'] = f'Error: {str(e)}'

@app.route('/')
def index():
    """Render main page"""
    return render_template('index.html')

@app.route('/extract', methods=['POST'])
def extract():
    """Start extraction job"""
    youtube_url = request.form.get('youtube_url')
    interval_seconds = int(request.form.get('interval_seconds', 10))
    quality = int(request.form.get('quality', 85))
    
    # Validate inputs
    if not youtube_url:
        return jsonify({'error': 'YouTube URL is required'}), 400
    
    if not ('youtube.com' in youtube_url or 'youtu.be' in youtube_url):
        return jsonify({'error': 'Please enter a valid YouTube URL'}), 400
    
    if interval_seconds < 1 or interval_seconds > 60:
        return jsonify({'error': 'Interval must be between 1 and 60 seconds'}), 400
    
    # Create job
    job_id = str(int(time.time()))
    jobs[job_id] = {
        'status': 'pending',
        'message': 'Starting extraction...',
        'progress': 0,
        'youtube_url': youtube_url,
        'interval_seconds': interval_seconds
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
    """Download PDF"""
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
            shutil.rmtree(temp_dir, ignore_errors=True)
        del jobs[job_id]
        return jsonify({'message': 'Cleanup successful'})
    return jsonify({'error': 'Job not found'}), 404

@app.route('/health')
def health():
    """Health check endpoint for deployment"""
    return jsonify({'status': 'healthy', 'python_version': '3.13.7'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)