// k6 load on the public price API, sent straight to API Gateway (bypassing the CloudFront
// cache) so every request reaches Lambda + DynamoDB BatchGetItem. Use it to show read
// capacity auto-scaling on the dashboard ("SATURATION - DynamoDB capacity").
// Run: k6 run -e OUTPUTS=.deploy-outputs-dev.json load/prices_load.js
import http from 'k6/http';
import { check } from 'k6';

const outputs = __ENV.OUTPUTS ? JSON.parse(open(`../${__ENV.OUTPUTS}`)) : {};
const BASE = __ENV.API_URL || outputs.listings_api;
const CROPS = ['MZE', 'SOY', 'GNT', 'BNS', 'RCE', 'CAS', 'MML'];

export const options = {
  scenarios: {
    burst: { executor: 'constant-arrival-rate', rate: 15, timeUnit: '1s', duration: '12m', preAllocatedVUs: 10, maxVUs: 40 },
  },
  thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<800'] },
};

export default function () {
  const c = CROPS[Math.floor(Math.random() * CROPS.length)];
  // 3 markets = 3 eventually consistent reads (0.5 RCU each) = 1.5 RCU per request, ~22 RCU/s total: well above the
  // 2-RCU floor, so target tracking (70%) scales the table up within a few minutes.
  const res = http.get(`${BASE}/api/prices?c=${c}&m=LSK,NDL,KTW`);
  check(res, { 'status 200': (r) => r.status === 200 });
}
