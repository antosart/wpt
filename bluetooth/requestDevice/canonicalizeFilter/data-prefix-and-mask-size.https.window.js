// META: script=/resources/testharness.js
// META: script=/resources/testharnessreport.js
// META: script=/resources/testdriver.js
// META: script=/resources/testdriver-vendor.js
// META: script=/bluetooth/resources/bluetooth-test.js
// META: script=/bluetooth/resources/bluetooth-fake-devices.js
'use strict';
const test_desc =
    'Manufacturer data mask size must be equal to dataPrefix size.';

let filters = [{
  manufacturerData: [{
    companyIdentifier: 0x0001,
    dataPrefix: new Uint8Array([0x01, 0x02, 0x03, 0x04]),
    mask: new Uint8Array([0xff]),
  }],
}];

bluetooth_test(
    (t) => promise_rejects_js(
        t, TypeError, requestDeviceWithTrustedClick({filters})),
    test_desc);
