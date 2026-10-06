import asyncio
from bleak import BleakClient
A='11:89:9A:A3:1C:29'; C='0000ffe1-0000-1000-8000-00805f9b34fb'
async def main():
 q=asyncio.Queue()
 async with BleakClient(A,timeout=20) as c:
  await c.start_notify(C,lambda _,x:q.put_nowait(bytes(x)))
  async def xmit(b,prefix,timeout=2):
   while not q.empty(): q.get_nowait()
   await c.write_gatt_char(C,b,response=False)
   end=asyncio.get_event_loop().time()+timeout
   while asyncio.get_event_loop().time()<end:
    try:r=await asyncio.wait_for(q.get(),end-asyncio.get_event_loop().time())
    except asyncio.TimeoutError:return None
    if r.startswith(prefix):return r
  print('initial 3D', (await xmit(bytes.fromhex('8c3d'),bytes.fromhex('8c3d'))).hex(' '))
  print('manual mode 41 00', ((await xmit(bytes.fromhex('8c4100'),bytes.fromhex('8c41'))) or b'').hex(' '))
  for i in [6,7,8,9,10]:
   ack=await xmit(bytes([0x8c,0x3e,i]),bytes.fromhex('8c3e'))
   rb=await xmit(bytes.fromhex('8c3d'),bytes.fromhex('8c3d'))
   print(f'index {i}: 3E ack={(ack or b" ").hex(" ")}  3D readback={(rb or b" ").hex(" ")}')
  print('restore auto 41 01', ((await xmit(bytes.fromhex('8c4101'),bytes.fromhex('8c41'))) or b'').hex(' '))
  print('final 3D', ((await xmit(bytes.fromhex('8c3d'),bytes.fromhex('8c3d'))) or b'').hex(' '))
asyncio.run(main())
