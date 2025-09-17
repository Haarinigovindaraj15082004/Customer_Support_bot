-- to do any operation in the database, this file can be used
-- DELETE FROM tickets WHERE id = 11;
BEGIN TRANSACTION;


UPDATE orders
SET status = 'ORDER_PLACED'
WHERE status = 'NEW';


INSERT INTO orders (order_id, status, shipping_address) VALUES

  ('ORDL3101', 'ORDER_PLACED', '14 Willow Ct, Southside'),
  ('ORDL3102', 'ORDER_PLACED', '9 Sunview Rd, Midtown'),
  ('ORDL3103', 'ORDER_PLACED', '88 Crescent Blvd, Westpark'),
  ('ORDL3104', 'ORDER_PLACED', '3 Jasmine Alley, North End'),
  ('ORDL3105', 'ORDER_PLACED', '120 Harbor View, Seaside'),
  ('ORDL3106', 'ORDER_PLACED', '47 Cedar Ridge, Hillcrest'),
  ('ORDL3107', 'ORDER_PLACED', '5 Orchard Walk, Old Town'),
  ('ORDL3108', 'ORDER_PLACED', '29 Lantern Way, Lakeside'),
  ('ORDL3201', 'SHIPPED', '702 Riverbend Dr, Riverside'),
  ('ORDL3202', 'SHIPPED', '18 Meadow Ln, Greenfield'),
  ('ORDL3203', 'SHIPPED', '63 Maple Grove, Brookwood'),
  ('ORDL3204', 'SHIPPED', '4 Stonebridge Pl, Eastgate'),
  ('ORDL3205', 'SHIPPED', '91 Aurora Ave, Skyline'),
  ('ORDL3206', 'SHIPPED', '250 Oak Terrace, Elmwood'),
  ('ORDL3301', 'DELIVERED', '17 Magnolia Cir, Rosewood'),
  ('ORDL3302', 'DELIVERED', '808 Beacon St, Harborview'),
  ('ORDL3303', 'DELIVERED', '221 Birch St, Fairview'),
  ('ORDL3304', 'DELIVERED', '44 Poppy Pl, Garden District'),
  ('ORDL3305', 'DELIVERED', '301 Summit Rd, Highpoint'),
  ('ORDL3306', 'DELIVERED', '12 Windmill Row, Millfield'),
  ('ORDL3401', 'CANCELLED', '6 Coral Reef Ct, Bayshore'),
  ('ORDL3402', 'CANCELLED', '77 Nightingale Way, Parkside'),
  ('ORDL3403', 'CANCELLED', '15 Copper Leaf Dr, Canyon Ridge'),
  ('ORDL3404', 'CANCELLED', '409 Starling Ave, Meadowbrook');

COMMIT;
