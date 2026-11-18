from app.modules.m7_archive.export_out import checksum

def execute(state):
    clock=state.get('clock_calibration',{})
    state['archive']={'sha256':checksum(state),'evidence_count':len(state.get('calculations',[]))+len(state['quarantine']),
        'ready':state.get('reconcile',{}).get('balanced',False),
        'clock_calibration_batch':clock.get('batch_id'),'clock_calibration_status':clock.get('status','not_applied')}
    return state
