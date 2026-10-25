-- Runs once when the Postgres volume is first created. Tests use this database so they never touch dev data.
create database swipebills_test owner swipebills;
