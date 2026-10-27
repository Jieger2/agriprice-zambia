// k6 load test for the USSD journey (auto-scaling + latency SLO evidence).
// Run: k6 run -e OUTPUTS=.deploy-outputs-dev.json load/ussd_load.js
//  or: k6 run -e USSD_URL="https://.../live/ussd?k=TOKEN" load/ussd_load.js
import http from 'k6/http';
import { check, sleep } from 'k6';

const outputs = __ENV.OUTPUTS ? JSON.parse(open(`../${__ENV.OUTPUTS}`)) : {};
const URL = __ENV.USSD_URL || outputs.ussd_callback;

export const options = {
  scenarios: {
    farmers: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '2m', target: 10 },
        { duration: '5m', target: 50 },   // drives DynamoDB consumed capacity past the 70% target
        { duration: '3m', target: 50 },
        { duration: '1m', target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.005'],     // availability SLO 99.5%
    http_req_duration: ['p(95)<1000'],   // latency SLO
  },
};

const params = { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } };

function step(session, phone, text) {
  const res = http.post(URL, { sessionId: session, serviceCode: '*384*1#', phoneNumber: phone, text }, params);
  check(res, {
    'status 200': (r) => r.status === 200,
    'CON/END': (r) => r.body.startsWith('CON ') || r.body.startsWith('END '),
    'fits 182 chars': (r) => r.body.length <= 182,
  });
  return res;
}

export default function () {
  const session = `k6-${__VU}-${__ITER}-${Date.now()}`;
  const phone = `+26097${String(1000000 + ((__VU * 7919 + __ITER) % 8999999)).padStart(7, '0')}`;
  const crop = 1 + (__ITER % 7);
  step(session, phone, '');
  sleep(1);
  step(session, phone, '1');
  sleep(1);
  step(session, phone, `1*${crop}`);
  sleep(2);
}
