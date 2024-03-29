#!/usr/bin/env python
# coding: utf-8

# # Moдель колонны для очистки воздуха от трития
#     - Код написан максимально просто иногда даже в угоду эффективности для предания простоты интепретации его
#     
# ### Заметки по релизу:
#     - Добавлен параметр: вероятность того, что молекула останется в той же ячейке (т.е. как бы добежит до конца ячейки и вернется в начало). Вероятность остаться в ячейке не зависит от свойств ячейки (константа для всех ячеек)
#     - добавлено орошение
#     - Добавлена блокировка связей
# In[3]:


import pandas as pd
import numpy as np
from tqdm import tqdm
import multiprocessing as mp
from itertools import repeat
import datetime 
from scipy.stats import bernoulli # по-умолчанию, колонна заполнена равномерно, каждая ячейка с одинаковой вероятностью может содержать катализатор
import matplotlib.pyplot as plt




# # Реализация модели колонны
# Основной вопрос: как создать модель цилиндрической колонны?
#  - Идея: сначала рисуем квадратное сечение, потом его заполняем окружностями
#  - Потом все окружности, чьи центры не вошли во вписанную в квадрат окружность отмечаем как уже заполненные (несмачиваемые).
#  - Оставшиеся окружности будем использовать далее в модели, как свободное пространство в колонне перед началом эксперимента. 


# количество заблокированных связей для ячейки
class LockedWaysData:
    ways = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    def __init__(self, n_locked_ways):
        if n_locked_ways>0:
            self.locked_ways = [self.ways[i] for i in np.random.choice(len(self.ways), n_locked_ways, replace=False)]
        else:
            self.locked_ways = []

# вспомогательаня функция для расчёта перцентилей
def percentile(n):
    def percentile_(x):
        return np.percentile(x, n)
    percentile_.__name__ = 'percentile_%s' % n
    return percentile_
 
# вспомогательная функция, чтобы создавать трехмерные структуры из ячеек (тензоры)
def cartesian_product(*arrays):
    la = len(arrays)
    dtype = np.result_type(*arrays)
    arr = np.empty([len(a) for a in arrays] + [la], dtype=dtype)
    for i, a in enumerate(np.ix_(*arrays)):
        arr[...,i] = a
    return arr.reshape(-1, la)  
 
# функция создаёт колонну
def create_pillar(lvDiameter=13, lvHeight=75, lvLen=2,n_locked_ways=0):
    a = cartesian_product(np.arange(0, lvDiameter), np.arange(0, lvDiameter), np.arange(0, lvHeight))
 
    pillar_df = pd.DataFrame({'length': a[:,0], 
                       'width': a[:,1], 
                       'height': a[:,2],
                       'status': [0]*lvDiameter*lvDiameter*lvHeight})

    
    # Маркируем те ячейки, центры которых не содержатся во вписанной в окружности 
    R = 0.5*lvDiameter*lvLen # центр сечения квадрата находится с координатами (R, R)
    pillar_df['distance from center']=[np.sqrt((1.0*lvLen*(pillar_df.loc[x,'length']+0.5)-R)**2+(1.0*lvLen*(pillar_df.loc[x, 'width']+0.5)-R)**2) for x in pillar_df.index]
    pillar_df['free_cell_flg'] = [pillar_df.loc[x,'distance from center']<=R for x in pillar_df.index]
    pillar_df['volume'] = 0

    # Отключение части проходов у ячейек
    cells = pillar_df[pillar_df.free_cell_flg == True].index
    for c in cells:
        pillar_df.loc[c, "locked_ways"] = LockedWaysData(n_locked_ways)

    # для удобства работы, делаем сводную таблицу из датафрейма
    pillar_df.set_index(['length', 'width', 'height'], inplace = True)
    
    
    return pillar_df
 
# вспомогательная функция ищет номера соседних нижних ячеек для данной
def neighbour_cells(cell, stepZ, df, status=[0], free_cell_flg=[True],stepXY = [-1,0,1]):
    final_list = []
    
    maxX =maxY = df.index.get_level_values('width').max()
    maxZ = df.index.get_level_values('height').max()
    
    if cell and cell[2]+stepZ<= maxZ:                           # проверка, что ячейка не пустая и аппликата не максимальна
        for i in stepXY:                                          #
            if cell[0]+i>=0 and cell[0]+i<maxX:
                for j in stepXY:
                    if (cell[1]+j>=0 and cell[1]+j<maxY                            # не вылазим за диапазон
                        and df.status.loc[cell[0]+i, cell[1]+j, cell[2]+stepZ] in status            # ищем ячейки только нужного статуса
                        and df.free_cell_flg.loc[cell[0]+i, cell[1]+j, cell[2]+stepZ] in free_cell_flg  # ищем ячейки только нужного типа
                        and [i, j, stepZ]!=[0,0,0]        # в ответе не нужна текущая ячейка
                        and (i, j) not in df.loc[(cell[0],cell[1],cell[2]+stepZ),'locked_ways'].locked_ways): # не используем заблокированную связь

                        final_list += [tuple(np.array(cell)+np.array([i, j, stepZ]))]
    return final_list

#находит соседние ячейки ближние к центру
def get_closer_to_init_cell(near_cells, cur_cell,init_cell):
    final_list = []
    for c in near_cells:
        if (abs(c[0]-init_cell[0])-abs(cur_cell[0]-init_cell[0]))<=0 and (abs(c[1]-init_cell[1])-abs(cur_cell[1]-init_cell[1]))<=0:
            final_list.append(c)
    return final_list

# функция находит, нет ли свободных ячеек, у которых все соседние нижние являются заняты катализатором (ситуации типа "пробка")
def check_locked_cells(df):
    not_empty_cells = df[df.status==1].index
 
    locked_cells = []
    
    for cell in not_empty_cells:
        n_cells = neighbour_cells(cell, stepZ=0, df=df, status=[0]) # список соседних ячеек
 
        # если все соседние ячейки являются заполненными катализатором (и это не последний слой), тогда добавляем эту ячейку в список заблокированных 
        if not n_cells: 
            locked_cells += [tuple(cell)]
 
    return locked_cells
 

# функция заполняем колонну катализатором случайным образом
def put_catalyst(df, lvInitX, lvInitY,irrigation, lvCatalystShare=1.0/5, draw = False):
    
    # работаем только с заполненными ячейками    
    empty_cells = df['free_cell_flg'] == True

    # случайным образом распределяем катализатор по свободным ячейкам колонны: 0 (нет катализатора) и 1 (есть катализатор)
    df.loc[empty_cells, 'status'] = bernoulli.rvs(p=lvCatalystShare, size=empty_cells.sum()) #



    # объём всех незаполненных ячеек определяем нулём
    df.loc[empty_cells, 'volume'] = 0 # 
 
    # статистика по катализатору
    if draw:
        print('Всего заполнено ячеек = ' + str(df.loc[empty_cells, 'status'].sum()))
        print('Доля заполненных катализатором ячеек = ', str(df.loc[empty_cells, 'status'].sum()/empty_cells.sum()))
 
    # инициализация: помещаем каплю, удаляем катализатор из начальной ячейки [mpInitX, mpInitY, 0] (если он там был)
    df.loc[(lvInitX,lvInitY,0),'status'] = 0
    df.loc[(lvInitX,lvInitY,0),'volume'] = 1
 
    
    # находим все заблокированные ячейки
    locked_cells = check_locked_cells(df)
 
    # если есть такие, то делаем отверстие в "пробке" путём удаления катализатора из случайной учейки из числа соседних нижних
    for c in locked_cells:
        df.loc[c,'status'] = 0 # изменяем статус этой ячейки, в дальнейшем можно рандомно выбирать ячейку из окружения 
  
    # размечаем пустые ячейки
    free_cells = np.array(df.free_cell_flg==True)
 
    # размечаем ячейки без катализатора
    cells_wo_catalyst = np.array(df.status ==0)
    
    # бежим с вернего уровня до нижнего и считаем объём потока в каждой ячейке
    height = df.index.get_level_values('height').max()

    # распределяем поток в колонне
    for z in np.arange(height):
        for c in df[(df.index.get_level_values('height') == z ) & free_cells & cells_wo_catalyst].index:
            if df.loc[c, "volume"] < 1 / irrigation:
                n_cells = neighbour_cells(c, stepZ=1, df=df, stepXY=[0])
                if n_cells:
                    df.loc[n_cells, 'volume'] += df.loc[c, 'volume'] / len(n_cells)
                else:
                    n_cells = neighbour_cells(c, stepZ=1, df=df)
                    n_cells = get_closer_to_init_cell(n_cells,c,(lvInitX,lvInitY,0))
                    if n_cells:
                        df.loc[n_cells, 'volume'] += df.loc[c, 'volume'] / len(n_cells)
                    else:
                        n_cells = neighbour_cells(c, stepZ=1, df=df)
                        if n_cells:
                            df.loc[n_cells, 'volume'] += df.loc[c, 'volume'] / len(n_cells)
                        else:
                            if draw:
                                print('Ячейка ', c, ' не содержит потока.')

            else:
                n_cells = neighbour_cells(c, stepZ=1, df=df)
                if n_cells:
                    df.loc[n_cells, 'volume'] += df.loc[c, 'volume'] / len(n_cells)
                else:
                    if draw:
                        print('Ячейка ', c, ' не содержит потока.')
    df.status = 1
    df.loc[df.volume > 0,'status'] = 0

    # проверяем, объем поток по каждому сечению (долен быть равен 1 для каждого уровня)
    if draw:
        df[['volume']].groupby('height').sum().plot(figsize=(25,5))
    return df

def get_time(i,init_cell,height,df,lvLeaveProbability,lvH):
    current_сell = init_cell
    time = 0
    # бежим в цикле по уровням колонны (сверху вниз)
    for z in np.arange(height):

        # собираем коордианту z следующей ячейки
        #             next_cell[2]=z+1
        n_cells = neighbour_cells(current_сell, stepZ=1, df=df)

        # выбираем случайным образом ячейку из тех, что свободны
        cell_no = np.random.randint(low=0, high=len(n_cells))

        leave_flg = np.random.rand() <= lvLeaveProbability
        time += 1.0 / df.loc[tuple(current_сell), 'volume']

        while not leave_flg:
            leave_flg = np.random.rand() <= lvLeaveProbability
            time += 1.0 * lvH / df.loc[tuple(current_сell), 'volume']

        current_сell = n_cells[cell_no]  # переходим в следующую ячейку (меняем тип переменной с array на list)
    return time


# Моделирование скорости потока
def try_sample(df, init_cell,lvLeaveProbability,irrigation, lvH = 1.0,  lvSampleSize = 100):
    '''
    df - <pandas dataframe> - колонна 
    init_cell int[3, 1] - координаты начальное ячейки, куда запускают струю
    lvProbabilityLeave - <float in [0,1]> - вероятность трассера покинуть ячейку после её прохождения 
    lvH <int> - уровень задержки на насадке
    lvSampleSize  - <int> - размер выборки (количество трассеров в эксперименте)
    '''
        
    # создаем массив длиной mpSampleSize, куда будем записывать время прохождения молекул через колонну
    #time_df = pd.DataFrame({'time':np.array([0]*lvSampleSize)})
    height = df.index.get_level_values('height').max()
    ss = np.arange(lvSampleSize)

    with mp.Pool(mp.cpu_count()) as pool:
        mp.freeze_support()
        time_list = pool.starmap(get_time, zip(ss,repeat(init_cell),repeat(height),repeat(df),
                                               repeat(lvLeaveProbability),repeat(lvH)))
        pool.close()
        pool.join()

        return time_list

    # бежим в цикле, чтобы записать результаты по каждому сэмплу (сэмпл - прогон молекулы через колонну)
    for i in time_df.index:
        # инициилизируем ячейку, с которой начинается движение молекулы
          current_сell = init_cell
 
          # бежим в цикле по уровням колонны (сверху вниз)
          for z in np.arange(height):
 
                # собираем коордианту z следующей ячейки
                n_cells = neighbour_cells(current_сell, stepZ=1, df=df)

                # выбираем случайным образом ячейку из тех, что свободны
                cell_no = np.random.randint(low = 0, high=len(n_cells))
                
                leave_flg = np.random.rand() <= lvLeaveProbability
                time_df.loc[i, 'time'] += 1.0/df.loc[tuple(current_сell), 'volume']
                
                while not leave_flg:
                        leave_flg = np.random.rand() <= lvLeaveProbability
                        time_df.loc[i, 'time'] += 1.0*lvH/df.loc[tuple(current_сell), 'volume']               
                
                current_сell = n_cells[cell_no]    # переходим в следующую ячейку (меняем тип переменной с array на list)
 
    # добавляем время прохождения самого нижнего уровня
    time_df.loc[i, 'time'] += 1.0/df.loc[tuple(current_сell), 'volume'] 
    
    return time_df
 
def run_experiment(lvDiameters = [13], lvHeights=[75], lvCatalystShares = [1.0/5], lvPutCatalystTries = 2,
                   lvHs = [1],
                   lvLeaveProbabilities = [0.5], lvSampleSizes=[1000, 10000], lvLen = 2.0, draw=False,
                   irrigation=[50],n_locked_ways = [0],save_pillar_df=False):
    '''lvLen = 2.0                 # шаг ячейки (например, 2 мм).
    lvDiameter = 13             # Диаметр основания (цилиндрической) колонны, пересчитанное на число ячеек (например, 13 ячеек)
    lvHeight = 75               # Высота колонны, пересчитанное на число ячеек (например, 75 ячеек)
    lvCatalystShare = 1.0/5     # Доля гидрофобного катализатора в колонне
    lvPutCatalystTries = 2      # сколько раз будем по разному заполнять колонну катализатором
    lvSampleSize = 1000         # сколько различных молекул будет пропущено по колонне
    ''' 
    
    time_stat = pd.DataFrame(columns=['diameter', 'height', 'catalystshare', 'try', 'H' ,'leave_probability' , 'sample_size', 'time'])
    for d in tqdm(lvDiameters, desc='цикл по диаметрам', ):
        for h in tqdm(lvHeights, desc='цикл по высоте колонны'):
            for nlw in tqdm(n_locked_ways, desc='цикл по заблокированным проходам'):
                pillar = create_pillar(lvDiameter=d, lvHeight=h, lvLen=lvLen,n_locked_ways=nlw)

                lvInitX = d//2     # абсцисса ячейки, в которую запускают струю
                lvInitY = d//2     # ордината ячейки, в которую запускают струю

                for sh in tqdm(lvCatalystShares, desc='цикл по доле катализатора'):

                    for ct in tqdm(range(lvPutCatalystTries), desc='цикл по тому, как по-разному заполнили колонну'):
                        for ir in tqdm(irrigation, desc='цикл по орошению'):
                            # Внимание: самый времязатратный шаг
                            pillar = put_catalyst(df=pillar, lvInitX=lvInitX, lvInitY=lvInitY,irrigation=ir, lvCatalystShare=sh, draw = draw)
                            if save_pillar_df:
                                dttime = str(datetime.datetime.today())[:-7].replace(':', '_')
                                path = 'pillar_df_file' + dttime + '.csv'
                                pillar.to_csv(path, sep=';', decimal=',')

                            for l in tqdm(lvHs, desc='цикл по задержкам на насадке колонну'):
                                for p in tqdm(lvLeaveProbabilities, desc='цикл по вероястности покинуть ячейку'):
                                    for ss in tqdm(lvSampleSizes, desc='цикл по трассерам'):
                                        time = pd.DataFrame({'time': np.array([0] * ss)})

                                        res = try_sample(df=pillar, init_cell =(lvInitX, lvInitY, 0), lvLeaveProbability=p,
                                                         irrigation=ir, lvH = l, lvSampleSize = ss)

                                        time['time'] = res
                                        time['diameter'] = d
                                        time['height'] = h
                                        time['catalystshare']=sh
                                        time['H']=l
                                        time['leave_probability'] = p
                                        time['try'] = ct
                                        time['sample_size'] = ss
                                        time['irrigation'] = ir
                                        time['n_locked_ways'] = nlw

                                        time_stat = time_stat.append(time)
                            # print(time_stat.shape)
                        
    return time_stat


# Запуск экспериментов

if __name__=="__main__":
    time = run_experiment(lvDiameters = [23], lvHeights=[200], lvCatalystShares = [0.2],
                          lvPutCatalystTries = 1, lvLeaveProbabilities = [0.42],
                          lvHs = [0.06], lvSampleSizes=[10000],irrigation=[395],n_locked_ways = [0,1,2,3],
                          draw=False,save_pillar_df=True)
    time = run_experiment(lvDiameters = [13], lvHeights=[5], lvCatalystShares = [1.0/5], lvPutCatalystTries = 1, lvSampleSizes=[1000], draw=False,irrigation=[50],n_locked_ways = [0,1,2],save_pillar_df=True)

    dttime = str(datetime.datetime.today())[:-7].replace(':', '_')
    path = 'time_csv_file' + dttime + '.csv'
    time.to_csv(path, sep=';', decimal=',')

    time_stat = (time.groupby(['diameter', 'height', 'catalystshare', 'leave_probability', 'H', 'sample_size']).
                agg({percentile(5), percentile(25), percentile(50), percentile(75), percentile(95)
                , 'mean', 'median', 'max', 'min', 'std', 'skew'})
                 )

    path = 'time_stat_file' + dttime + '.csv'
    time_stat.to_csv(path, sep=';', decimal=',')









