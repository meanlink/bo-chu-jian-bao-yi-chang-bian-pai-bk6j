import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const seals={list:()=>api('/audit-seals'),create:note=>api('/audit-seals',{method:'POST',body:JSON.stringify({note:note||''})}),retry:id=>api('/audit-seals/'+id+'/retry',{method:'POST'}),verifyChain:()=>api('/audit-seals/verify'),verifyDocument:document=>api('/audit-seals/verify',{method:'POST',body:JSON.stringify({document})})};
