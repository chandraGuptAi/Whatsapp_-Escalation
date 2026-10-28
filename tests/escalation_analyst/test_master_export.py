"""Master-history Excel downloads resolve child group tables, not the prose master."""
import io
import time
from openpyxl import load_workbook
from .test_core import client, ingest


def test_master_report_download_uses_its_cluster_and_period(client):
    ingest(client)
    r=client.app.state.runtime
    now=int(time.time())
    def save(group,scope,body,start,end,cluster=None):
        return r.reports.save(dict(group_id=group,group_name=group,scope=scope,report=body,period_start=start,period_end=end,message_count=1,window_label='test',cluster_id=cluster))
    table='| AWB | Status |\n| --- | --- |\n| 12345678 | Delayed |'
    save('test@g.us','group',table,now-100,now)
    save('test@g.us','group',table.replace('12345678','87654321'),now-1000,now-500)
    master=save('__master__:all','master','Narrative master without a table',now-100,now,'all')
    response=client.get('/master/bulk-export',params={'reportId':master,'clusterId':'wrong-ui-selection'})
    assert response.status_code==200,response.text
    rows=list(load_workbook(io.BytesIO(response.content),data_only=True).active.values)
    assert rows[0][:5] == ('Company', 'Time', 'Seller', 'No. of Escalation Entries', 'AWB')
    assert len(rows) == 2 and rows[1][0] == 'test@g.us' and rows[1][4] == '12345678'
    preview = client.get(f'/master/reports/{master}/rows').json()
    assert preview['rows'][0]['awb'] == '12345678'
