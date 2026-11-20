/** @typedef {{id:number,branch:string,day:string,volume:number,note:string}} DailyVolume */
/** @typedef {{id:number,name:string,branch:string,backtest_days:number,alpha:number,beta:number,state:string,result:object}} ForecastRun */
/** @typedef {{id:number,run_id:number,capacity:number,model:string,schedule:object[],status:string}} Staffing */
/** @typedef {{number:string,state:string,state_label:string,branch?:string,epsilon?:number,daily_cap?:number,days?:string[],forecasts?:{seasonal_naive:number[],holt:number[]},mechanism?:object,charged_days?:number,error?:string}} Publication */
