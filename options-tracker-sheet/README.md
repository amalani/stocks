# stocks
Scripts for stock market access

## Loss stop in the Recommendation column (Chain Summary `AR`)

The sheet's `AR` formula has take-profit, ITM and delta-rising branches but no loss stop. Add this branch first (paste into
`AR5` and fill down; `AL` is Premium Captured %, so `AL <= -1` means the put now costs at least 2x the credit you took):

```
=IF($W5<>"Open","",IF(AND($AL5<>"",$AL5<=-1),"STOP - put is 2x premium, close or roll for a credit",IF(AND($AH5<>"",$AH5="ITM"),"ITM - Assignment Risk",IF(AND($AL5<>"",$AL5>=0.9),"Close Today - 90%+ Captured",IF(AND($AL5<>"",$AL5>=0.8,$H5<>"",$H5<=10),"Consider Closing",IF(AND($H5<>"",$H5<=3),"Near Expiry - Monitor",IF(AND($Z5<>"",$Z5>=0.35),"Delta Rising - Watch","Hold")))))))
```

It only works while Market Data has a live price for the chain. A gap can jump past 2x before the next refresh, so size
volatile names for a larger loss. Roll only for a net credit.
