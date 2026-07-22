"""Render all report charts as polished PNGs (matplotlib)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

NAVY="#1F3A5F"; TEAL="#2E8B8B"; GOLD="#C9962A"; SLATE="#64748B"; RED="#C0392B"; GREEN="#1E8449"
plt.rcParams.update({
 "font.family":"DejaVu Sans","font.size":11,"axes.edgecolor":"#D5DBE1",
 "axes.labelcolor":NAVY,"axes.titlesize":13,"axes.titleweight":"bold",
 "axes.titlecolor":NAVY,"xtick.color":"#22303C","ytick.color":"#22303C",
 "figure.facecolor":"white","axes.facecolor":"white"})

def save(fig,name):
    fig.savefig(f"/home/claude/charts/{name}.png",dpi=150,bbox_inches="tight")
    plt.close(fig)

def declutter(ax):
    for s in ["top","right"]: ax.spines[s].set_visible(False)

# ---------- data ----------
ALLOC=[("Equity — India",44),("Equity — US",26),("MF — India",14),("Gold & Silver",7),("Crypto",6),("Other",3)]
GEO=[("India",58),("United States",32),("Global",10)]
CAP=[("Large cap",38),("Mid cap",21),("Small cap",29),("Micro / other",12)]
SECTORS=[("Technology",34),("Financials",17),("Commodities",12),("Industrials",11),("Consumer",9),("Energy",8),("Other",9)]
TOP=[("Nvidia",11.5),("Parag Parikh\nFlexi",8.2),("TCS",6.4),("IVV\nS&P 500",6.1),("Gold\nBeES",5.3),("QQQ",4.9),("Motilal\nMidcap",4.4),("Jio\nFinancial",4.1)]
BETAS=[("Bitcoin",2.6),("Nvidia",1.9),("Kotak Small Cap",1.3),("IVV S&P 500",1.0),("Parag Parikh",0.9),("TCS",0.7),("Gold BeES",0.1)]
OVER=[("NASDAQ 100 × FANG+",55),("IVV × QQQ",42),("Parag Parikh × Smallcap 250",9)]
COSTS=[("Axis Global FoF",1.24),("ICICI Manufacturing",0.98),("Parag Parikh",0.63),("Motilal Midcap",0.57),("Kotak Small Cap",0.44),("Tata Digital",0.31)]
RADAR=[("Equity",70,55),("Debt",0,20),("Gold",7,10),("International",32,15),("Cash/Alt",6,0)]
SCORE=64

PALETTE=[TEAL,NAVY,GOLD,"#7FB3B3","#9AA8BD","#D9C08A","#B9C7C7"]

# 1. gauge
fig,ax=plt.subplots(figsize=(4.2,2.6),subplot_kw={"aspect":"equal"})
ax.pie([SCORE,100-SCORE],startangle=90,counterclock=False,
       colors=[TEAL,"#E7ECF0"],wedgeprops=dict(width=0.32))
ax.text(0,0.05,f"{SCORE}",ha="center",va="center",fontsize=30,fontweight="bold",color=NAVY)
ax.text(0,-0.28,"/100",ha="center",va="center",fontsize=11,color=SLATE)
ax.set_title("Diversification score")
save(fig,"gauge")

# 2. allocation donut
fig,ax=plt.subplots(figsize=(6.2,3.6))
vals=[v for _,v in ALLOC]; labs=[f"{n}\n{v}%" for n,v in ALLOC]
ax.pie(vals,labels=labs,colors=PALETTE,startangle=90,counterclock=False,
       wedgeprops=dict(width=0.42),textprops={"fontsize":9.5})
ax.set_title("By asset class")
save(fig,"alloc")

# 3. geo pie
fig,ax=plt.subplots(figsize=(5.6,3.4))
vals=[v for _,v in GEO]; labs=[f"{n}  {v}%" for n,v in GEO]
ax.pie(vals,labels=labs,colors=[TEAL,NAVY,GOLD],startangle=90,counterclock=False,
       textprops={"fontsize":10})
ax.set_title("By geography")
save(fig,"geo")

# 4. market cap bar
fig,ax=plt.subplots(figsize=(6.2,3.4))
names=[n for n,_ in CAP]; vals=[v for _,v in CAP]
bars=ax.bar(names,vals,color=TEAL,width=0.55)
ax.bar_label(bars,fmt="%d%%",padding=2,fontsize=10,color=NAVY,fontweight="bold")
ax.set_ylabel("Weight %"); ax.set_ylim(0,max(vals)*1.2)
ax.set_title("By company size"); declutter(ax); ax.tick_params(axis="x",rotation=0)
save(fig,"cap")

# 5. radar
dims=[d for d,_,_ in RADAR]; you=[a for _,a,_ in RADAR]; ref=[b for _,_,b in RADAR]
ang=np.linspace(0,2*np.pi,len(dims),endpoint=False).tolist(); ang+=ang[:1]
you+=you[:1]; ref+=ref[:1]
fig,ax=plt.subplots(figsize=(5.6,4.4),subplot_kw=dict(polar=True))
ax.plot(ang,you,color=TEAL,lw=2,label="You"); ax.fill(ang,you,color=TEAL,alpha=.30)
ax.plot(ang,ref,color=GOLD,lw=2,ls="--",label="Balanced reference"); ax.fill(ang,ref,color=GOLD,alpha=.12)
ax.set_xticks(ang[:-1]); ax.set_xticklabels(dims,fontsize=10)
ax.set_yticks([20,40,60]); ax.set_yticklabels(["20%","40%","60%"],fontsize=8,color=SLATE)
ax.set_title("Allocation shape: You vs Balanced",pad=18)
ax.legend(loc="lower center",bbox_to_anchor=(0.5,-0.22),ncol=2,frameon=False,fontsize=10)
save(fig,"radar")

# 6. pareto (single % axis; bars + cumulative line + 50% marker)
fig,ax=plt.subplots(figsize=(8.2,4.0))
names=[n for n,_ in TOP]; vals=[v for _,v in TOP]; cum=np.cumsum(vals)
bars=ax.bar(names,vals,color=TEAL,width=0.6,label="Holding weight")
ax.bar_label(bars,fmt="%.1f",padding=2,fontsize=9,color=NAVY)
ax.plot(names,cum,color=GOLD,lw=2.5,marker="o",ms=5,label="Cumulative")
ax.axhline(50,color=RED,lw=1,ls=":")
ax.text(len(names)-0.4,51.5,"50% of portfolio",color=RED,fontsize=9,ha="right")
ax.set_ylabel("% of portfolio"); ax.set_ylim(0,62)
ax.set_title("Top-8 holdings and how fast they add up (Pareto)")
ax.legend(frameon=False,loc="upper left",fontsize=10)
declutter(ax); plt.setp(ax.get_xticklabels(),fontsize=9)
save(fig,"pareto")

# 7. sector barh
fig,ax=plt.subplots(figsize=(6.8,3.8))
names=[n for n,_ in SECTORS][::-1]; vals=[v for _,v in SECTORS][::-1]
bars=ax.barh(names,vals,color=[TEAL if v<30 else GOLD for v in vals],height=0.6)
ax.bar_label(bars,fmt="%d%%",padding=3,fontsize=10,color=NAVY,fontweight="bold")
ax.set_xlabel("Weight %"); ax.set_xlim(0,max(vals)*1.18)
ax.axvline(30,color=RED,lw=1,ls=":"); ax.text(30.5,len(names)-0.6,"30% comfort line",color=RED,fontsize=8.5)
ax.set_title("Sector split (gold bar = above comfort line)"); declutter(ax)
save(fig,"sector")

# 8. overlap barh
fig,ax=plt.subplots(figsize=(6.8,2.9))
names=[n for n,_ in OVER][::-1]; vals=[v for _,v in OVER][::-1]
cols=[GREEN if v<20 else (GOLD if v<40 else RED) for v in vals]
bars=ax.barh(names,vals,color=cols,height=0.5)
ax.bar_label(bars,fmt="%d%%",padding=3,fontsize=10,fontweight="bold")
ax.set_xlabel("Estimated overlap %"); ax.set_xlim(0,68)
ax.axvline(20,color=SLATE,lw=1,ls=":"); ax.axvline(40,color=RED,lw=1,ls=":")
ax.text(20.5,-0.45,"healthy <20%",fontsize=8.5,color=SLATE)
ax.text(40.5,-0.45,"redundant >40%",fontsize=8.5,color=RED)
ax.set_title("Fund-pair overlap (colour = health)"); declutter(ax)
save(fig,"overlap")

# 9. beta barh
fig,ax=plt.subplots(figsize=(6.8,3.8))
names=[n for n,_ in BETAS][::-1]; vals=[v for _,v in BETAS][::-1]
cols=[TEAL if v<=1 else GOLD if v<2 else RED for v in vals]
bars=ax.barh(names,vals,color=cols,height=0.55)
ax.bar_label(bars,fmt="%.1f",padding=3,fontsize=10,fontweight="bold")
ax.axvline(1.0,color=NAVY,lw=1.2,ls="--"); ax.text(1.02,len(names)-0.5,"market = 1.0",fontsize=8.5,color=NAVY)
ax.set_xlabel("Beta vs NIFTY 50"); ax.set_xlim(0,3.0)
ax.set_title("Beta ladder (red = swings 2×+ the market)"); declutter(ax)
save(fig,"beta")

# 10. cost bar
fig,ax=plt.subplots(figsize=(7.0,3.6))
names=[n for n,_ in COSTS]; vals=[v for _,v in COSTS]
cols=[RED if v>0.9 else TEAL for v in vals]
bars=ax.bar(names,vals,color=cols,width=0.55)
ax.bar_label(bars,fmt="%.2f%%",padding=2,fontsize=9.5,fontweight="bold")
ax.axhline(0.3,color=GREEN,lw=1,ls=":"); ax.text(len(names)-.5,0.32,"index-fund zone",color=GREEN,fontsize=8.5,ha="right")
ax.set_ylabel("Expense ratio %"); ax.set_ylim(0,1.45)
ax.set_title("What each fund charges (red = expensive)")
declutter(ax); plt.setp(ax.get_xticklabels(),rotation=18,ha="right",fontsize=9)
save(fig,"cost")

# 11. fee drag line
yrs=list(range(1,11)); fees=[1000000*((1.0068)**i-1)/1000 for i in yrs]
fig,ax=plt.subplots(figsize=(6.4,3.2))
ax.plot(yrs,fees,color=GOLD,lw=2.5,marker="o",ms=4)
ax.fill_between(yrs,fees,color=GOLD,alpha=.15)
ax.annotate(f"₹{fees[-1]:.0f}k by Yr 10",xy=(10,fees[-1]),xytext=(6.4,fees[-1]*0.75),
            fontsize=10,color=NAVY,fontweight="bold",
            arrowprops=dict(arrowstyle="->",color=SLATE))
ax.set_xlabel("Year"); ax.set_ylabel("Cumulative fees (₹ '000)")
ax.set_title("Fee drag on ₹10L at your 0.68% weighted cost"); declutter(ax)
save(fig,"feedrag")

print("rendered:",__import__("os").listdir("/home/claude/charts"))
