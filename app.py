import os
import io
import argparse
import webbrowser
import math
from datetime import datetime # To automatically get the current date
from threading import Timer
from flask import Flask, render_template, request, flash, jsonify, session, redirect, url_for, Response
from weasyprint import HTML

app = Flask(__name__)
app.secret_key = 'a_final_secret_key_that_is_very_secure'
CMD_FILE_DATA = None

def parse_sdp_file(file_content):
    """
    Parses a SimpleDyno (.sdp) file with robust, multi-stage header detection.
    """
    lines = file_content.splitlines()
    config_data, performance_data = {}, []
    
    header_keys = ["Gear_Ratio", "Roller_Diameter", "Roller_Mass", "Actual_MOI"]
    for line in lines:
        if ":" in line:
            key, val = line.split(":", 1)
            if key.strip() in header_keys:
                config_data[key.strip()] = val.strip()
        if line.strip() == 'PRIMARY_CHANNEL_CURVE_FIT_DATA':
            break
            
    data_started = False
    header_found = False
    rpm_col, tq_col, pwr_col = 'RPM1_Motor_(rad/s)', 'Motor_Torque_(N.m)', 'Power_(W)'
    rpm_idx, tq_idx, pwr_idx = -1, -1, -1
    
    for line in lines:
        line = line.strip()
        if line == 'PRIMARY_CHANNEL_CURVE_FIT_DATA':
            data_started = True; continue
        if not data_started: continue
        if line.startswith('FULL_SET_COAST_DOWN_FIT_DATA'): break
        
        if not header_found and line.startswith('Time_(Sec)'):
            headers = line.split()
            try:
                rpm_idx, tq_idx, pwr_idx = headers.index(rpm_col), headers.index(tq_col), headers.index(pwr_col)
                header_found = True
            except ValueError as e:
                flash(f"Critical Error: A required column was not found: {e}"); return None
            continue

        if header_found and line:
            values = line.split()
            if len(values) <= max(rpm_idx, tq_idx, pwr_idx): continue
            try:
                rpm = float(values[rpm_idx].replace(',', '.')) * 9.5493
                if rpm < 5500: continue
                performance_data.append({
                    'rpm': round(rpm),
                    'torque': round(float(values[tq_idx].replace(',', '.')), 2),
                    'hp': round(float(values[pwr_idx].replace(',', '.')) / 745.7, 2)
                })
            except (ValueError, IndexError): continue

    if not performance_data:
        flash("Parsing complete, but no data points were found above 5500 RPM."); return None
    return {"config": config_data, "data": performance_data}

def aggregate_data_by_rpm(data, increment=500):
    """ Aggregates performance data into bins of a specified RPM increment. """
    if not data: return []
    bins = {}
    for point in data:
        bin_key = math.floor(point['rpm'] / increment) * increment
        if bin_key not in bins:
            bins[bin_key] = {'torque': [], 'hp': []}
        bins[bin_key]['torque'].append(point['torque'])
        bins[bin_key]['hp'].append(point['hp'])
        
    aggregated_results = []
    for rpm_key in sorted(bins.keys()):
        torques, hps = bins[rpm_key]['torque'], bins[rpm_key]['hp']
        avg_torque, avg_hp = sum(torques) / len(torques), sum(hps) / len(hps)
        aggregated_results.append({'rpm': rpm_key, 'torque': round(avg_torque, 2), 'hp': round(avg_hp, 2)})
    return aggregated_results

def process_file_content(content, filename):
    """ Processes the raw file content, parses it, and calculates all necessary metrics. """
    parsed = parse_sdp_file(content)
    if not parsed or not parsed.get("data"): return None
    data = parsed["data"]
    return {
        "config": parsed["config"], "data_points_count": len(data),
        "peak_power": max(data, key=lambda x: x['hp']),
        "peak_torque": max(data, key=lambda x: x['torque']),
        "torque_data": [{'x': r['rpm'], 'y': r['torque']} for r in data],
        "hp_data": [{'x': r['rpm'], 'y': r['hp']} for r in data],
        "raw_data": data, "filename": filename,
        "aggregated_data": aggregate_data_by_rpm(data)
    }

@app.route('/')
def main_page():
    global CMD_FILE_DATA
    if CMD_FILE_DATA:
        session['report_data'] = CMD_FILE_DATA
        data = CMD_FILE_DATA; CMD_FILE_DATA = None
        return render_template('results.html', **data)
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file_handler():
    main_file = request.files.get('main_file')
    if not main_file or main_file.filename == '':
        flash('A Main Run file is required.'); return render_template('index.html')
        
    # --- NEW: Process metadata from form ---
    report_meta = {
        'customer_name': request.form.get('customer_name', 'N/A'),
        'engine_type': request.form.get('engine_type', 'N/A'),
        'test_date': datetime.now().strftime('%Y-%m-%d %H:%M')
    }
    
    main_run_data = process_file_content(io.StringIO(main_file.stream.read().decode("utf-8")).read(), main_file.filename)
    if not main_run_data: return render_template('index.html')
    
    comp_run_data = None
    comp_file = request.files.get('comparison_file')
    if comp_file and comp_file.filename != '':
        comp_run_data = process_file_content(io.StringIO(comp_file.stream.read().decode("utf-8")).read(), comp_file.filename)
    
    # Store all data in the session for the PDF export
    session['report_data'] = {
        'main_run': main_run_data, 
        'comparison_run': comp_run_data,
        'meta': report_meta 
    }
    return render_template('results.html', main_run=main_run_data, comparison_run=comp_run_data, meta=report_meta)

@app.route('/export-pdf', methods=['POST'])
def export_pdf():
    report_data = session.get('report_data')
    chart_image = request.form.get('chartImage')
    if not report_data or not chart_image:
        flash("No data available to generate a report."); return redirect(url_for('main_page'))

    # Render the dedicated report template.
    # We no longer need to pass a special logo_path.
    html_string = render_template('report.html', 
                           main_run=report_data.get('main_run'), 
                           comparison_run=report_data.get('comparison_run'), 
                           meta=report_data.get('meta'),
                           chart_image=chart_image)
    
    # THE FIX: Use base_url to tell WeasyPrint how to find '/static/logo.png'
    pdf = HTML(string=html_string, base_url=request.url_root).write_pdf()
    
    return Response(pdf, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=dyno_report.pdf'})

def main_cli():
    # This function remains unchanged, but won't use the new metadata fields.
    global CMD_FILE_DATA
    parser = argparse.ArgumentParser(description="Parse and display SimpleDyno .sdp files.")
    parser.add_argument('filepaths', nargs='*', help="Path to main .sdp file, optionally a comparison .sdp file.")
    args = parser.parse_args()
    if args.filepaths:
        try:
            with open(args.filepaths[0], 'r') as f: main_data = process_file_content(f.read(), args.filepaths[0])
            if not main_data: print(f"Critical Error: Failed to parse main file: {args.filepaths[0]}."); return
            
            # Add default meta for command-line usage
            default_meta = {'customer_name': 'CLI User', 'engine_type': 'N/A', 'test_date': datetime.now().strftime('%Y-%m-%d')}
            CMD_FILE_DATA = {"main_run": main_data, "comparison_run": None, "meta": default_meta}

            if len(args.filepaths) > 1:
                with open(args.filepaths[1], 'r') as f: CMD_FILE_DATA["comparison_run"] = process_file_content(f.read(), args.filepaths[1])
            
            Timer(1, lambda: webbrowser.open_new("http://127.0.0.1:5000/")).start()
        except Exception as e: 
            print(f"An error occurred: {e}"); CMD_FILE_DATA = None
    app.run(host='127.0.0.1', port=5000)

if __name__ == '__main__':
    main_cli()