// META: script=/resources/testdriver.js
// META: script=/resources/testdriver-vendor.js
// META: script=/bluetooth/resources/bluetooth-test.js
// META: script=/bluetooth/resources/bluetooth-fake-devices.js
'use strict';
const test_desc = 'A read request succeeds and returns the secure ' +
    'characteristic\'s value.';
const EXPECTED_VALUE = [0, 1, 2];
const PERIPHERAL_PIN = '123456'
const PAIRING_PINS = ['123456'];

  const {characteristic, fake_characteristic} =
      await getMeasurementIntervalCharacteristic(PERIPHERAL_PIN);
  await fake_characteristic.setNextReadResponse(
      GATT_SUCCESS, EXPECTED_VALUE, PAIRING_PINS);
  const value = await characteristic.readValue();
  assert_array_equals(new Uint8Array(value.buffer), EXPECTED_VALUE)
}, test_desc);
